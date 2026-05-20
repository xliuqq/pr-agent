"""
Quick debug script for PR review - minimal version
Usage: python quick_debug_review.py
"""

import asyncio
import sys
from pathlib import Path

# Add project root to Python path
sys.path.insert(0, str(Path(__file__).parent))

from pr_agent.agent.pr_agent import PRAgent


async def main():
    """Quick PR review for debugging"""
    pr_url = "http://gitee.wty.cn/enterprises/2/projects/208/pull_requests/66"
    
    print(f"Reviewing PR: {pr_url}")
    
    # Initialize agent and run review
    agent = PRAgent()
    result = await agent.handle_request(pr_url, "/review")
    
    if result:
        print("✅ Review completed!")
    else:
        print("❌ Review failed!")


if __name__ == "__main__":
    asyncio.run(main())