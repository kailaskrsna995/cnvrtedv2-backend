"""
Seed a test user + profile with ICP into Supabase.
Run once: python seed_test_user.py
"""
import asyncio
from dotenv import load_dotenv
load_dotenv()

from app.database import supabase
from app.pipeline.matching import vectorise_text

# Test agency — performance marketing for SaaS
TEST_USER_EMAIL = "test@cnvrted.com"

ICP_TEXT = """
Target Industry: B2B SaaS, AI startups, tech companies
Company Size: Series A to Series C, 20-200 employees
Buyer Title: CMO, Head of Marketing, VP Growth, Founder
Pain Points: Need to scale paid acquisition fast after funding, no in-house marketing team yet, burning VC money without clear ROI on ads
Top Trigger Events:
1. Company just raised Series A or B funding (has budget, needs to spend it on growth)
2. Job posting for Head of Marketing open for 60+ days (can't hire, will outsource)
3. Founder posting about struggling with paid ads or growth stalling
Summary: Fast-growing SaaS companies that just raised and need to scale marketing immediately
"""

USER_CONTEXT = """
What they sell: Performance marketing services for B2B SaaS — paid ads, demand gen, growth strategy
Pricing tier: Mid-market ($5K-$20K/month retainers)
Tone: Direct, data-driven, ROI-focused
Key differentiators: SaaS-only focus, performance-based pricing, fast onboarding
Existing clients: Series A/B SaaS companies in fintech, HR tech, dev tools
"""

async def seed():
    print("Seeding test user...")

    # 1. Create user
    user_result = supabase.table("users").upsert({
        "email": TEST_USER_EMAIL,
    }, on_conflict="email").execute()

    user_id = user_result.data[0]["id"]
    print(f"User created: {user_id}")

    # 2. Generate ICP vector
    print("Generating ICP vector...")
    icp_vector = await vectorise_text(ICP_TEXT)
    print(f"Vector length: {len(icp_vector)}")

    # 3. Create profile with ICP
    profile_result = supabase.table("user_profiles").insert({
        "user_id": user_id,
        "name": "Performance Marketing Agency",
        "website_url": "https://example.com",
        "service_description": "Performance marketing for B2B SaaS",
        "target_description": "Series A-C SaaS companies that just raised and need to scale",
        "user_context": USER_CONTEXT,
        "icp_text": ICP_TEXT,
        "icp_vector": icp_vector,
    }).execute()

    profile_id = profile_result.data[0]["id"]
    print(f"Profile created: {profile_id}")
    print("Done. Test user seeded successfully.")
    print(f"\nUser ID:   {user_id}")
    print(f"Profile ID: {profile_id}")

asyncio.run(seed())
