from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
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

# Robust title getter (avoids 30s timeout on missing selectors)
def robust_get_title(page, url) -> str:
    """
    Try several selectors quickly and fall back gracefully to <title> or URL slug.
    """
    candidate_selectors = [
        ".episode-title-header h3",
        "header .episode-title-header h3",
        ".contentp h2",
        "h1",
        "h2",
    ]
    for sel in candidate_selectors:
        try:
            el = page.wait_for_selector(sel, timeout=3000, state="visible")
            txt = el.inner_text().strip()
            if txt:
                return txt
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue

    # Fallback 1: document title
    try:
        t = page.title().strip()
        if t:
            return t
    except Exception:
        pass

    # Fallback 2: build something from the URL slug
    try:
        slug = url.rstrip("/").split("/")[-1]
        slug = slug.replace("-", " ").strip()
        if slug:
            return slug
    except Exception:
        pass

    return "Untitled Episode"

# -- ACTUAL SCRAPER FUNCTIONS --

def exponential_backoff_wait(attempt_count):
    if attempt_count < len(BACKOFF_DELAYS):
        return BACKOFF_DELAYS[attempt_count]
    else:
        return BACKOFF_DELAYS[-1]  # Cap at 1 hour (3600 seconds)

def cloudflareCheck(page):
    # NOTE: -- Cloudflare blocking check with exponential backoff --
    attempt_count = 0
    # Using title check here because the challenge often sets it
    while True:
        try:
            title_text = page.title()
        except Exception:
            title_text = ""
        if "Cloudflare" not in title_text:
            break
        delay = exponential_backoff_wait(attempt_count)
        print(f"Cloudflare detected, waiting {delay} seconds (attempt {attempt_count + 1})")
        sleep(delay)
        try:
            page.reload()
        except Exception:
            pass
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
        page.set_default_timeout(60000)  # be a bit more patient overall
        page.goto(url, wait_until="domcontentloaded")

        # IMPORTANT: wait out Cloudflare before touching the DOM
        cloudflareCheck(page)

        # Use resilient title getter
        videoTitle = robust_get_title(page, url)

        # Check if file already exists
        safe_name = clean_title(videoTitle) + ".mp4"
        if os.path.exists(safe_name):
            print(f"File already exists, skipping: {safe_name}")
            browser.close()
            return {
                "title": videoTitle,
                "urls": set(),
            }

        # Try clicking the flash link if present; don't fail if not
        try:
            ph = page.locator(".flash_link").nth(1)
            ph.wait_for(state="visible", timeout=5000)
            for _ in range(3):
                with context.expect_page() as _new_page_info:
                    ph.click()
        except PlaywrightTimeoutError:
            print("Flash link not found/visible; continuing to scan iframes anyway.")
        except Exception as e:
            print(f"Error clicking flash links: {e}")

        # Wait just long enough for iframes/tabs to populate
        sleep(delay_time)

        for tab in context.pages:
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
                    # Isolate m3u8 file from the wrapper URL
                    m = re.search(r"(?<=\?url=).+$", url_placeholder)
                    if m:
                        downloadUrls.add(m.group(0))

        browser.close()

        # Return title + any candidate m3u8 URLs
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
