from tavily import TavilyClient
import os
from dotenv import load_dotenv

load_dotenv()

tavily_api_key = os.getenv("TAVILY_API_KEY")

client = TavilyClient(api_key=tavily_api_key)

def search_tavily(query: str) -> str:
    response = client.search(query=query, max_results=5)
    results = []
    for i, r in enumerate(response["results"], 1):
      title = r.get("title", "Unknown")
      url = r.get("url", "Unknown")
      content = r.get("content", "").strip()
      if len(content) > 300:
        content = content[:300].rsplit(" ", 1)[0] + "..."
      results.append(f"{i}. **{title}**\n  {url}\n  {content}\n\n")
    return "\n".join(results)