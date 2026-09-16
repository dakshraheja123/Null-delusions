from pathlib import Path
from google import genai
from google.genai import types


def load_api_key():
    key_file = Path(__file__).with_name("api_key.txt")
    if not key_file.exists():
        raise FileNotFoundError(
            "Create api_key.txt in the same folder as main.py and put your Gemini API key inside it."
        )

    api_key = key_file.read_text(encoding="utf-8").strip()
    if not api_key:
        raise ValueError("api_key.txt is empty.")

    return api_key


def load_skill_prompt():
    skill_path = Path(__file__).with_name("skill.md")
    if skill_path.exists():
        return skill_path.read_text(encoding="utf-8").strip()
    return "You are a helpful assistant."


def chat_with_gemini():
    api_key = load_api_key()
    client = genai.Client(api_key=api_key)

    skill_prompt = load_skill_prompt()
    chat = client.chats.create(
        model="gemini-3.6-flash",
        config=types.GenerateContentConfig(
            system_instruction=skill_prompt
        ),
    )

    print("Chat with Gemini")
    print("Type 'q' to quit.")

    while True:
        user_input = input("\nYou: ").strip()

        if user_input.lower() == "q":
            print("Goodbye!")
            break

        if not user_input:
            continue

        response = chat.send_message(user_input)
        print(f"\nGemini: {response.text}")


if __name__ == "__main__":
    chat_with_gemini()