import json
from groq import Groq
from test_groq import ask_groq


def generate_itinerary(destination, start_date, end_date, budget, interests, trip_personality):
    prompt = f"""Create a detailed day-by-day travel itinerary for a trip to {destination},
from {start_date} to {end_date}, with a total budget of {budget}.
The traveler is interested in: {interests}.
Trip style/personality: {trip_personality}.

For each day, suggest 2-4 activities that logically flow together based on location
(group nearby activities together rather than scattering them), and include a rough
estimated cost for each activity in the same currency as the budget.

Respond with ONLY valid JSON, in exactly this format, and nothing else
(no markdown, no explanation, no code fences):

[
  {{
    "day": 1,
    "date": "...",
    "activities": [
      {{"time": "...", "name": "...", "description": "...", "estimated_cost": 0}}
    ]
  }}
]
"""

    response_text = ask_groq(prompt)
    cleaned = response_text.replace("```json", "").replace("```", "").strip()
    itinerary = json.loads(cleaned)
    return itinerary

if __name__ == "__main__":
    result = generate_itinerary(
        destination="Ao Nang, Thailand",
        start_date="2026-03-01",
        end_date="2026-03-03",
        budget="$500",
        interests="beaches, local food, hiking",
        trip_personality="Relaxed + Foodie"
    )
    print(result)