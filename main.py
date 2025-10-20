from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from concurrent.futures import ThreadPoolExecutor, as_completed
from time import sleep
import re
import os
import unicodedata
import pickle

# =========================
# GLOBAL CONFIG
# =========================
delay_time = 5
BACKOFF_DELAYS = [2, 10, 60, 1800, 3600]  # Exponential backoff (2s, 10s, 1min, 30min, 1h)
TITLE_KEY = "title"
URL_KEY = "urls"
MIN_BYTES = 80 * 1024 * 1024  # 80 MB minimum file size

# How many episodes to download in parallel
MAX_PARALLEL_DOWNLOADS = 8  # set between 5–10 to taste

# ✅ Shows to skip (case-insensitive substring match)
SKIP_SHOWS = [ "Advocate Anjali Awasthi"]


class State:
    def __init__(self):
        self.showsInDownloadQueue = []
        self.currentURL = ""
        self.currentDownloadIndex = 0


# =========================
# HELPERS
# =========================
def clean_title(title: str) -> str:
    title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    title = title.lower().replace(" ", "_")
    title = re.sub(r'[^a-z0-9_-]', '', title).strip("_-")
    return title


def cleanContext(context):
    context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        window.chrome = {webstore: () => {}};
        Object.defineProperty(navigator, 'userAgent', {
            get: () => 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        });
        Object.defineProperty(window, 'innerWidth', {get: () => 1920});
        Object.defineProperty(window, 'innerHeight', {get: () => 1080});
    """)


def robust_get_title(page, url) -> str:
    candidate_selectors = [
        ".episode-title-header h3", "header .episode-title-header h3",
        ".contentp h2", "h1", "h2"
    ]
    for sel in candidate_selectors:
        try:
            el = page.wait_for_selector(sel, timeout=3000, state="visible")
            txt = el.inner_text().strip()
            if txt:
                return txt
        except Exception:
            continue
    try:
        t = page.title().strip()
        if t:
            return t
    except Exception:
        pass
    slug = url.rstrip("/").split("/")[-1].replace("-", " ").strip()
    return slug or "Untitled Episode"


def exponential_backoff_wait(attempt_count):
    return BACKOFF_DELAYS[min(attempt_count, len(BACKOFF_DELAYS) - 1)]


def cloudflareCheck(page):
    attempt_count = 0
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


# =========================
# CORE SCRAPER
# =========================
def extractDownloadUrlsFromEpisodePage(url):
    with sync_playwright() as p:
        downloadUrls = []
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(service_workers="block", accept_downloads=False)
        cleanContext(context)
        context.route("**/*disable-devtool", lambda route: route.abort())
        context.on("page", lambda new_page: new_page.route("**/*disable-devtool", lambda r: r.abort()))
        page = context.new_page()
        page.set_default_timeout(60000)
        page.goto(url, wait_until="domcontentloaded")
        cloudflareCheck(page)
        videoTitle = robust_get_title(page, url)

        safe_name = clean_title(videoTitle) + ".mp4"
        if os.path.exists(safe_name):
            size = os.path.getsize(safe_name)
            if size >= MIN_BYTES:
                print(f"✅ File already exists and is >= 80MB, skipping: {safe_name}")
                browser.close()
                return {"title": videoTitle, "urls": []}
            else:
                print(f"⚠️ Existing file too small ({size} bytes). Deleting and retrying: {safe_name}")
                try:
                    os.remove(safe_name)
                except Exception as e:
                    print(f"Failed to delete small file {safe_name}: {e}")

        # Try to click their popup/flash link if present
        try:
            ph = page.locator(".flash_link").nth(1)
            ph.wait_for(state="visible", timeout=5000)
            for _ in range(3):
                with context.expect_page() as _new_page_info:
                    ph.click()
        except Exception as e:
            print(f"Flash link not clickable/visible: {e}")

        sleep(delay_time)

        for tab in context.pages:
            if tab.is_closed():
                continue
            try:
                frames = tab.locator("iframe").all()
            except Exception:
                continue
            for x in frames:
                url_placeholder = x.get_attribute("src")
                if url_placeholder and ".m3u8" in url_placeholder:
                    m = re.search(r"(?<=\?url=).+$", url_placeholder)
                    if m:
                        candidate = m.group(0)
                        if candidate not in downloadUrls:
                            downloadUrls.append(candidate)

        browser.close()
        return {"title": videoTitle, "urls": downloadUrls}


def getTVShowTitles(url):
    with sync_playwright() as p:
        tvTitles = []
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(service_workers="block", accept_downloads=False)
        cleanContext(context)
        context.route("**/*disable-devtool", lambda route: route.abort())
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded")
        cloudflareCheck(page)
        for x in page.locator(".serial-list-wrap a").all():
            tvTitles.append(x.inner_text())
        return tvTitles


# =========================
# DOWNLOAD LOGIC (no aria2c)
# =========================
def download_with_ytdlp(title, download_url):
    """
    Download with yt-dlp only (no aria2c). Return True if file >= 80MB,
    else delete and return False so caller can try next link.
    """
    safe_name = clean_title(title) + ".mp4"
    print(f"🎬 Starting download: {safe_name}")

    # Add a bit of intrinsic parallelism for HLS fragments within a single episode
    cmd = (
        f'yt-dlp '
        f'--concurrent-fragments 10 -N 10 '
        f'-R infinite --fragment-retries infinite '
        f'-o "{safe_name}" "{download_url}"'
    )
    exit_code = os.system(cmd)
    print(f"yt-dlp exit code: {exit_code}")

    if not os.path.exists(safe_name):
        print(f"❌ File not found after download: {safe_name}")
        return False

    size = os.path.getsize(safe_name)
    if size >= MIN_BYTES:
        print(f"✅ Download completed ({size} bytes): {safe_name}")
        return True

    print(f"⚠️ Too small ({size} bytes): deleting and retrying")
    try:
        os.remove(safe_name)
    except Exception as e:
        print(f"Failed to delete small file {safe_name}: {e}")
    return False


def download_episode(episode_url):
    """
    Download a single episode, trying multiple m3u8 links if needed.
    Returns the episode title on success, or None on failure.
    """
    urlDict = extractDownloadUrlsFromEpisodePage(episode_url)
    title, urls = urlDict[TITLE_KEY], urlDict[URL_KEY]
    if not urls:
        print(f"⚠️ No URLs found for episode: {episode_url}")
        return None

    for idx, download_url in enumerate(urls, start=1):
        print(f"➡️ Trying link {idx}/{len(urls)} for episode: {title}")
        if download_with_ytdlp(title, download_url):
            print(f"✅ Episode '{title}' downloaded successfully.")
            return title
        else:
            print(f"🔁 Retrying with next link for '{title}'...")

    print(f"❌ All links failed for: {title}")
    return None


# =========================
# PAGE-BY-PAGE + PARALLEL
# =========================
def processShowPageByPage(showName):
    """
    Process a show page by page, downloading episodes in parallel
    to maximize bandwidth.
    """
    showName = showName.strip().replace(" ", "-")
    print(f"\n🎞️ Processing show: {showName}")
    pageNum = 1

    while True:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context(service_workers="block", accept_downloads=False)
            cleanContext(context)
            context.route("**/*disable-devtool", lambda route: route.abort())
            page = context.new_page()
            page.goto(f"https://apnetv.biz/Hindi-Serial/episode/{showName}/dd/{pageNum}",
                      wait_until="domcontentloaded")
            cloudflareCheck(page)
            links = page.locator(".shows-box a").all()
            hrefs = [a.get_attribute("href") for a in links if a.get_attribute("href")]
            browser.close()

        if not hrefs:
            break

        print(f"📺 Found {len(hrefs)} episodes on page {pageNum}. Starting parallel downloads (max {MAX_PARALLEL_DOWNLOADS})...")

        # Run downloads in parallel
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL_DOWNLOADS) as executor:
            futures = {executor.submit(download_episode, url): url for url in hrefs}
            for future in as_completed(futures):
                result = future.result()
                if result:
                    print(f"✅ Finished: {result}")
                else:
                    print(f"⚠️ Failed: {futures[future]}")

        sleep(delay_time)
        pageNum += 1

    print(f"🏁 Completed show: {showName}")


# =========================
# MAIN
# =========================
def run():
    state = State()
    filename = "state.pkl"

    if os.path.exists(filename):
        with open(filename, "rb") as f:
            state = pickle.load(f)
        if not state.showsInDownloadQueue:
            return

    state.showsInDownloadQueue = getTVShowTitles("https://apnetv.biz/Hindi-Serials")

    with open(filename, "wb") as f:
        pickle.dump(state, f)

    for show_name in state.showsInDownloadQueue[:]:
        # Skip list (case-insensitive substring match)
        if any(skip.lower() in show_name.lower() for skip in SKIP_SHOWS):
            print(f"⏭️ Skipping show: {show_name}")
            continue

        print(f"\n=== Starting to process show: {show_name} ===")
        try:
            processShowPageByPage(show_name)
            print(f"=== Completed: {show_name} ===\n")
        except Exception as e:
            print(f"⚠️ Error processing {show_name}: {e}")
            continue

        state.showsInDownloadQueue.remove(show_name)
        with open(filename, "wb") as f:
            pickle.dump(state, f)


if __name__ == "__main__":
    run()
    print("__DONE__")
