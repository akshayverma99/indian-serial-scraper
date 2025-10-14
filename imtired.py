from playwright.sync_api import sync_playwright
from uuid import uuid4
from time import sleep
import re
import os
import unicodedata
import pprint

# GLOBAL VARIABLES
delay_time = 1

# Exponential backoff configuration
BACKOFF_DELAYS = [2, 10, 60, 1800, 3600]  # 2s, 10s, 1min, 30min, 1hour

# ----------
# ----------
# -- KEYS --
# ----------
# ----------
# STATE KEYS
SHOWS_KEY = "shows"
NUMBER_OF_SHOWS_KEY = "numberOfShows"
CURRENT_URL_KEY = "currentUrl"
CURRENT_VIDEO_INDEX_KEY = "currentVideoIndex"
# SHOW KEYS
NUM_OF_DOWNLOADED_EPISODES_KEY = "numOfDownloadedEpisodes"
NUM_OF_ACTUAL_EPISODES_KEY = "numOfActualEpisodes"
EPISODES_KEY = "episodes"
NAME_OF_SHOW_KEY = "nameOfShow"
# --
EPISODE_NAME_KEY = "episodeName"
EPISODE_URL_KEY = "episodeUrl"
EPISODE_DOWNLOADED_KEY = "episodeDownloaded"
EPISODE_PATH_TO_FILE_KEY = "episodePathToFile"
# --



class state:
    def __init__(self):
        self.shows: [show] = []
        self.currentUrl = ""
        self.currentVideoIndex = 0

    def toJSON(self):
        return {
            SHOWS_KEY: [show.toJSON() for show in self.shows],
            NUMBER_OF_SHOWS_KEY: len(self.shows),
            CURRENT_URL_KEY: self.currentUrl,
            CURRENT_VIDEO_INDEX_KEY: self.currentVideoIndex,
        }
    
    def fromJSON(self, stateJson):
        self.shows = [show().fromJSON(showJson) for showJson in stateJson[SHOWS_KEY]]
        self.currentUrl = stateJson[CURRENT_URL_KEY]
        self.currentVideoIndex = stateJson[CURRENT_VIDEO_INDEX_KEY]
        return self

class show:
    def __init__(self):
        self.numOfActualEpisodes = 0
        self.nameOfShow = ""
        self.episodes: [episode] = []

    def toJSON(self):
        return {
            NUM_OF_DOWNLOADED_EPISODES_KEY: len(self.episodes),
            NUM_OF_ACTUAL_EPISODES_KEY: self.numOfActualEpisodes,
            EPISODES_KEY: [episode.toJSON() for episode in self.episodes],
            NAME_OF_SHOW_KEY: self.nameOfShow
        }
    
    def fromJSON(self, showJson):
        self.numOfActualEpisodes = showJson[NUM_OF_ACTUAL_EPISODES_KEY]
        self.nameOfShow = showJson[NAME_OF_SHOW_KEY]
        self.episodes = [episode().fromJSON(episodeJson) for episodeJson in showJson.get(EPISODES_KEY, [])]
        return self

class episode:
    def __init__(self):
        self.episodeName: str = ""
        self.pathToFile: str = ""

    def toJSON(self):
        return {
            EPISODE_NAME_KEY: self.episodeName,
            EPISODE_PATH_TO_FILE_KEY: self.pathToFile,
        }

    def fromJSON(self, episodeJson):
        self.episodeName = episodeJson[EPISODE_NAME_KEY]
        self.pathToFile = episodeJson[EPISODE_PATH_TO_FILE_KEY]
        return self


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

# -- ACTUAL SCRAPER FUNCTIONS --

def exponential_backoff_wait(attempt_count):
    """
    Returns the appropriate delay time based on attempt count.
    Progression: 2s → 10s → 1min → 30min → 1hour (capped at 1hour)
    """
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
            "urls": list(downloadUrls)
        }

# Gets Every TV Show Title (don't need direct links because we're gonna use their url structure instead)
def getTVShowTitles(url):
    with sync_playwright() as p:
        tvTitles = []
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(service_workers="block", accept_downloads=False)

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
            browser.close()
        
        # If no links, we've reached the end
        if len(links) == 0:
            break
            
        print(f"Processing page {pageNum} with {len(links)} episodes")
        
        # Process each episode one by one
        for i, link_element in enumerate(links):
            episode_url = link_element.get_attribute("href")
            if episode_url:
                print(f"Processing episode {i+1}/{len(links)} on page {pageNum}: {episode_url}")
                
                # Extract download URLs for this episode in a separate browser context
                try:
                    result = extractDownloadUrlsFromEpisodePage(episode_url)
                    if result["urls"]:
                        # Download the first available URL
                        download_url = result["urls"][0]
                        title = result.get("title")
                        
                        if not title:
                            print(f"No title found for episode, skipping: {episode_url}")
                            continue
                        
                        print(f"Downloading: {title}")
                        download_success = download_with_ytdlp(title, download_url)
                        
                        if not download_success:
                            print(f"Download failed for: {title}")
                    else:
                        print(f"No download URLs found for: {episode_url}")
                except Exception as e:
                    print(f"Error processing episode {episode_url}: {e}")
                    continue
        
        # Move to next page only after all episodes on current page are downloaded
        pageNum += 1
        print(f"Completed page {pageNum - 1}, moving to page {pageNum}")
        
        # Small delay between pages to be respectful
        sleep(delay_time)
    
    print(f"Completed processing all pages for show: {showName}")

def getAllShowsDownloadLinks(showName):
    """Legacy function - kept for compatibility but now just calls processShowPageByPage"""
    showName = showName.lstrip().rstrip().replace(" ","-")
    print(f"Legacy call to getAllShowsDownloadLinks for: {showName}")
    # This function is now deprecated in favor of processShowPageByPage
    return []

def download_with_ytdlp(title, download_url):
    safe_name = clean_title(title) + ".mp4"
    print(f"Starting download: {safe_name}")
    os.system("yt-dlp -o " + safe_name + " " + download_url)
    print(f"Download completed: {safe_name}")
    return True

# -- MAIN METHODS --

def run(state):
    # Get all TV show titles
    titles = getTVShowTitles("https://apnetv.biz/Hindi-Serials")
    
    # Process each show page by page, downloading immediately
    for show_name in titles:
        print(f"\n=== Starting to process show: {show_name} ===")
        try:
            processShowPageByPage(show_name)
            print(f"=== Completed processing show: {show_name} ===\n")
        except Exception as e:
            print(f"Error processing show {show_name}: {e}")
            continue


if __name__ == "__main__":

    # TODO = Pull the state from file

    # run()
    # Create a state
    stateTest = state()

    showTest = show()
    showTest.nameOfShow = "Gravity Falls"
    # Create a show
    episodeOne = episode()
    episodeOne.episodeName = "Episode 1"
    episodeOne.pathToFile = "Episode 1.mp4"

    episodeTwo = episode()
    episodeTwo.episodeName = "Episode 2"
    episodeTwo.pathToFile = "Episode 2.mp4"

    showTest.episodes.append(episodeOne)
    showTest.episodes.append(episodeTwo)

    stateTest.shows.append(showTest)

    showTestTwo = show()
    showTestTwo.nameOfShow = "Gravity Falls 2"
    showTestTwo.episodes.append(episodeOne)
    showTestTwo.episodes.append(episodeTwo)
    stateTest.shows.append(showTestTwo)

    # pprint.pp(stateTest.toJSON())

    stateTest.fromJSON(stateTest.toJSON())
    pprint.pp(stateTest.toJSON())


    # for show in getAllShowsDownloadLinks("Ishani"):
    #     # downloadVideoAt(show)
    #     infiniteWait()



    # This flow doesnt work but this is what it'll look like
    # -------------------------------------------------------
    # -------------------------------------------------------
    # titles = getTVShowTitles()
    # for title in titles:
    #     links = getAllShowsDownloadLinks()
    #     for link in links:
    #         getPageDownloadUrls(link)
    # -------------------------------------------------------
    # -------------------------------------------------------



    pass