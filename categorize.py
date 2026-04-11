from pathlib import Path

from dotenv import load_dotenv
from jinja2 import Template
from openai import OpenAI
from pydantic import BaseModel

import os

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
_prompt_template: Template | None = None


def _get_template() -> Template:
    global _prompt_template
    if _prompt_template is None:
        path = Path(__file__).parent / "prompts" / "category_mappings.md"
        _prompt_template = Template(path.read_text())
    return _prompt_template


class CategoryMapping(BaseModel):
    category: str


def categorize(sender_email: str, sender_name: str, subject: str) -> str:
    prompt = _get_template().render(
        sender_email=sender_email,
        sender_name=sender_name,
        subject=subject,
    )
    response = client.beta.chat.completions.parse(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        response_format=CategoryMapping,
    )
    return response.choices[0].message.parsed.category
