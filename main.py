import requests
from sectors import us_sector_etfs
import os
from dotenv import load_dotenv

load_dotenv()

class Sector_Investor: 
    def perplexity_call():
        api_key = os.getenv("PERPLEXITY_API_KEY")
        if not api_key:
            return "Missing PERPLEXITY_API_KEY in .env"

        # Define the headers for authentication.
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

        prompt = (
            "From this dictionary and keeping the current market conditions in mind, "
            "what is the best sector to be currently in (Be direct, and return only the sector name and UK Ticker in JSON) (Limit your search range to the dictionary provided): "
            + ", ".join(us_sector_etfs.values())
        )

        # Perplexity uses chat-completions payload format.
        data = {
            "model": "sonar",
            "messages": [
                {
                    "role": "user",
                    "content": prompt
                }
            ]
        }

        # Make a POST request to the Perplexity API.
        response = requests.post(
            "https://api.perplexity.ai/chat/completions",
            headers=headers,
            json=data,
            timeout=30
        )

        # Parse and return the response.
        if response.status_code == 200:
            result = response.json()
            return result.get("choices", [{}])[0].get("message", {}).get("content", "No answer found")
        return f"Error {response.status_code}: {response.text}"
        
if __name__ == "__main__":
    insights = Sector_Investor.perplexity_call()
    print("Answer:", insights)

    