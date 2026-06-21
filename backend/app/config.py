from dotenv import load_dotenv
import os

load_dotenv(override=True)

# Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

# Anthropic
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# OpenAI (embeddings only)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Agent APIs
PRAW_CLIENT_ID = os.getenv("PRAW_CLIENT_ID", "")
PRAW_CLIENT_SECRET = os.getenv("PRAW_CLIENT_SECRET", "")
PRAW_USER_AGENT = os.getenv("PRAW_USER_AGENT", "cnvrted/2.0")
SERPER_API_KEY = os.getenv("SERPER_API_KEY", "")
EXA_API_KEY = os.getenv("EXA_API_KEY", "")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
APIFY_API_TOKEN = os.getenv("APIFY_API_TOKEN", "")
# Cookieless LinkedIn actors (override via env to swap)
APIFY_PEOPLE_SEARCH_ACTOR = os.getenv("APIFY_PEOPLE_SEARCH_ACTOR", "powerai/linkedin-peoples-search-scraper")
APIFY_EMPLOYEES_ACTOR = os.getenv("APIFY_EMPLOYEES_ACTOR", "apimaestro/linkedin-company-employees-scraper-no-cookies")
APIFY_LINKEDIN_ACTOR = os.getenv("APIFY_LINKEDIN_ACTOR", "apimaestro/linkedin-posts-search-scraper")

# Notion export (founder's workspace via internal integration token)
NOTION_API_KEY = os.getenv("NOTION_API_KEY", "")
NOTION_PARENT_PAGE_ID = os.getenv("NOTION_PARENT_PAGE_ID", "")

# Enrichment
HUNTER_API_KEY = os.getenv("HUNTER_API_KEY", "")
FULLENRICH_API_KEY = os.getenv("FULLENRICH_API_KEY", "")
APOLLO_API_KEY = os.getenv("APOLLO_API_KEY", "")

# Thresholds
VECTOR_SIMILARITY_THRESHOLD = float(os.getenv("VECTOR_SIMILARITY_THRESHOLD", "0.70"))
INTENT_SCORE_THRESHOLD = float(os.getenv("INTENT_SCORE_THRESHOLD", "0.60"))
MAX_LEADS_PER_DAY = int(os.getenv("MAX_LEADS_PER_DAY", "20"))
