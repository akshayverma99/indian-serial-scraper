from playwright.sync_api import sync_playwright
from time import sleep
import re
import os
import unicodedata
import pickle

# GLOBAL VARIABLES
delay_time = 5

# Exponential backoff configuration
BACKOFF_DELAYS = [2, 10, 60, 1800, 3600]  # 2s, 10s, 1min, 30min, 1hour

# dict keys
TITLE_KEY = "title"
URL_KEY = "urls"

class State:
    def __init__(self):
        self.showsInDownloadQueue = []
        self.currentURL = ""
        self.currentDownloadIndex = 0


# -- HELPER FUNCTIONS --

# Cleans strings for the terminal
def clean_title(title: str) -> str:
    # Normalize Unicode (e.g. é → e)
    title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()

    # Lowercase
    title = title.lower()

    # Replace spaces with underscores
    title = title.replace(" ", "_")

    # Remove everything not alphanumeric, dash, or underscore
    title = re.sub(r'[^a-z0-9_-]', '', title)

    # Remove leading/trailing junky underscores or dashes
    title = title.strip("_-")

    return title

def infiniteWait():
    while True:
        sleep(delay_time)

def cleanContext(context):
    # set headers that mimic a real browser
    context.add_init_script("""
               Object.defineProperty(navigator, 'webdriver', {get: () => undefined})
               window.chrome = {webstore: () => {}}
           """)

    # Set user agent to a real browser
    context.add_init_script("""
               Object.defineProperty(navigator, 'userAgent', {get: () => 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'})
           """)

    # Set viewport size to a real browser
    context.add_init_script("""
               Object.defineProperty(window, 'innerWidth', {get: () => 1920})
               Object.defineProperty(window, 'innerHeight', {get: () => 1080})
           """)

# -- ACTUAL SCRAPER FUNCTIONS --

def exponential_backoff_wait(attempt_count):
    if attempt_count < len(BACKOFF_DELAYS):
        return BACKOFF_DELAYS[attempt_count]
    else:
        return BACKOFF_DELAYS[-1]  # Cap at 1 hour (3600 seconds)

def cloudflareCheck(page):
    # NOTE: -- Cloudflare blocking check with exponential backoff --
    attempt_count = 0
    while "Cloudflare" in page.title():
        delay = exponential_backoff_wait(attempt_count)
        print(f"Cloudflare detected, waiting {delay} seconds (attempt {attempt_count + 1})")
        sleep(delay)
        page.reload()
        attempt_count += 1

# -- PLAYWRIGHT FUNCTIONS --

# Extracts download URLs for a single episode page (no download here)
def extractDownloadUrlsFromEpisodePage(url):
    with sync_playwright() as p:
        downloadUrls = set()
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(service_workers="block", accept_downloads=False)
        videoTitle = ""
        cleanContext(context)

        # Stops their immediate browser closing
        def block_disable_devtool(route):
            route.abort()

        # Apply to initial context
        context.route("**/*disable-devtool", block_disable_devtool)

        # When a new page (popup) is created, hook the same route
        context.on("page", lambda new_page: new_page.route("**/*disable-devtool", block_disable_devtool))

        page = context.new_page()
        page.goto(url,
                  wait_until="domcontentloaded")

        if "apnetv.biz" in page.url:
            videoTitle = page.locator(".episode-title-header h3").inner_text()
        else:
            videoTitle = page.locator(".contentp h2").first.inner_text()
        
        # Check if file already exists
        safe_name = clean_title(videoTitle) + ".mp4"
        if os.path.exists(safe_name):
            print(f"File already exists, skipping: {safe_name}")
            browser.close()
            return {
                "title": videoTitle,
                "urls": set(),
            }
        
        ph = page.locator(".flash_link").nth(1)
        for x in range(3):
            with context.expect_page() as new_page_info:
                ph.click()

        # Cant wait for the dom to load fully because they keep it perma-loading
        # so we wait just long enough for everything to load
        sleep(delay_time)

        for tab in context.pages:
            # print(tab.locator("iframe"))

            # Silent error handling (not important)
            if tab.is_closed():
                continue
            try:
                frames = tab.locator("iframe").all()
            except Exception as e:
                # Tab or browser got closed, skip silently
                print(e)
                continue

            for x in frames:
                url_placeholder = x.get_attribute("src")
                if (url_placeholder is not None) and (".m3u8" in url_placeholder):
                    # Fancy regex magic that basically isolates their m3u8 file from the url
                    # If you keep the original url they require a bunch of fancy permissions but the m3u8 file
                    # server doesnt give a fuck
                    downloadUrls.add(re.search(r"(?<=\?url=).+$", url_placeholder).group(0))

        browser.close()

        #FIXME: Add an error check and throw something up if that video doesnt have any download urls
        return {
            "title": videoTitle,
            "urls": downloadUrls,
        }

# Gets Every TV Show Title (don't need direct links because we're gonna use their url structure instead)
def getTVShowTitles(url):
    with sync_playwright() as p:
        tvTitles = []
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(service_workers="block", accept_downloads=False)
        cleanContext(context)

        # Stops their immediate browser closing
        def block_disable_devtool(route):
            route.abort()
        # Apply to initial context
        context.route("**/*disable-devtool", block_disable_devtool)
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded")
        cloudflareCheck(page)
        for x in page.locator(".serial-list-wrap a").all():
            tvTitles.append(x.inner_text())
        return tvTitles

def processShowPageByPage(showName):
    """
    Process a show page by page, downloading episodes immediately to avoid rate limiting.
    This way, by the time downloads finish, any rate limiting will have expired.
    """
    showName = showName.lstrip().rstrip().replace(" ","-")
    print(f"Processing show: {showName}")

    pageNum = 1
    
    while True:
        # Get all episode links from the page first
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context(service_workers="block", accept_downloads=False)
            cleanContext(context)

            # Stops their immediate browser closing
            def block_disable_devtool(route):
                route.abort()

            # Apply to initial context
            context.route("**/*disable-devtool", block_disable_devtool)
            page = context.new_page()
            
            # Navigate to the page
            page.goto("https://apnetv.biz/Hindi-Serial/episode/" + str(showName) +"/dd/"+str(pageNum), wait_until="domcontentloaded")
            cloudflareCheck(page)
            
            # Get all episode links on this page
            links = page.locator(".shows-box a").all()

            # Extract hrefs before closing the browser
            hrefs = []
            for a in links:
                href = a.get_attribute("href")
                if not href:
                    print("No href found for link: " + a.inner_text())
                hrefs.append(href)

            browser.close()
        
        # If no links, we've reached the end
        if len(hrefs) == 0:
            break

        for episode_url in hrefs:
            # download every link and pop it
            # Once its empty go to next page
            # Do that all here instead of the outside loop so the above triggers when the page is actually empty
            # -------------- MARK: WAS WORKING HERE --------------
            urlDict = extractDownloadUrlsFromEpisodePage(episode_url)
            title = urlDict[TITLE_KEY]
            urls = urlDict[URL_KEY]
            if not urls:
                continue
            download_url = next(iter(urls))

            download_with_ytdlp(title, download_url)
            

        
        # Small delay between pages to be respectful
        sleep(delay_time)
        pageNum += 1
    
    print(f"Completed processing all pages for show: {showName}")

# def getAllShowsDownloadLinks(showName):
#     """Legacy function - kept for compatibility but now just calls processShowPageByPage"""
#     showName = showName.lstrip().rstrip().replace(" ","-")
#     print(f"Legacy call to getAllShowsDownloadLinks for: {showName}")
#     # This function is now deprecated in favor of processShowPageByPage
#     return []

def download_with_ytdlp(title, download_url):
    safe_name = clean_title(title) + ".mp4"
    print(f"Starting download: {safe_name}")
    os.system("yt-dlp -o " + safe_name + " " + download_url)
    print(f"Download completed: {safe_name}")
    return True

def TEST_download_with_ytdlp(title, download_url):
    print("__DOWNLOADING__")
    sleep(20)

# -- MAIN METHODS --

def run():
    state = State()
    filename = "state.pkl"

    # only activates in theres a save file
    if os.path.exists(filename):
        with open(filename, "rb") as f:
            state = pickle.load(f)
        if len(state.showsInDownloadQueue) == 0:
            return


    # Get all TV show titles
    state.showsInDownloadQueue = getTVShowTitles("https://apnetv.biz/Hindi-Serials")
    
    # Pickle the titles
    with open(filename, "wb") as f:
        pickle.dump(state, f)

    # Process each show page by page, downloading immediately
    for show_name in state.showsInDownloadQueue:
        print(f"\n=== Starting to process show: {show_name} ===")
        try:
            processShowPageByPage(show_name)
            print(f"=== Completed processing show: {show_name} ===\n")
        except Exception as e:
            print(f"Error processing show {show_name}: {e}")
            continue
        # Remove the current show_name from the titles list and re-pickle
        state.showsInDownloadQueue.remove(show_name)
        with open(filename, "wb") as f:
            pickle.dump(state, f)


if __name__ == "__main__":

    run()
    print("__DONE__")

    # Get all shows

    # Go to episode page
    # REPEAT -> Download the episode
    # Go to the next page

    # if crashed then what does it need? Just the page and the download index

    # d = extractDownloadUrlsFromEpisodePage("https://apnetv.biz/Hindi-Serial/show/274408/Bhabi-Ji-Ghar-Par-Hai")
    # print(d[URL_KEY].pop())
    # download_with_ytdlp(d[TITLE_KEY], d[URL_KEY].pop())

    # https://apnetv.biz/Hindi-Serial/show/273342/Aami-Dakini
    # https://apnetv.biz/Hindi-Serial/show/274187/Mahabharat

    pass