<picture>
  <source media="(prefers-color-scheme: light)" srcset="https://github.com/user-attachments/assets/2ccdb752-22fb-41c7-8948-857fc1ad7e24"">
  <source media="(prefers-color-scheme: dark)" srcset="https://github.com/user-attachments/assets/774a46d5-27a0-490c-b7d0-e65fcbbfa358">
  <img alt="Shows a black Browser Use Logo in light color mode and a white one in dark color mode." src="https://github.com/user-attachments/assets/2ccdb752-22fb-41c7-8948-857fc1ad7e24"  width="full">
</picture>

<div align="center">
    <picture>
    <source media="(prefers-color-scheme: light)" srcset="https://github.com/user-attachments/assets/9955dda9-ede3-4971-8ee0-91cbc3850125"">
    <source media="(prefers-color-scheme: dark)" srcset="https://github.com/user-attachments/assets/6797d09b-8ac3-4cb9-ba07-b289e080765a">
    <img alt="The AI browser agent." src="https://github.com/user-attachments/assets/9955dda9-ede3-4971-8ee0-91cbc3850125"  width="400">
    </picture>
</div>

<div align="center">
<a href="https://cloud.browser-use.com"><img src="https://media.browser-use.tools/badges/package" height="48" alt="Browser-Use Package Download Statistics"></a>
</div>

---

<div align="center">
<a href="#demos"><img src="https://media.browser-use.tools/badges/demos" alt="Demos"></a>
<img width="16" height="1" alt="">
<a href="https://docs.browser-use.com"><img src="https://media.browser-use.tools/badges/docs" alt="Docs"></a>
<img width="16" height="1" alt="">
<a href="https://browser-use.com/posts"><img src="https://media.browser-use.tools/badges/blog" alt="Blog"></a>
<img width="16" height="1" alt="">
<a href="https://browsermerch.com"><img src="https://media.browser-use.tools/badges/merch" alt="Merch"></a>
<img width="100" height="1" alt="">
<a href="https://github.com/browser-use/browser-use"><img src="https://media.browser-use.tools/badges/github" alt="Github Stars"></a>
<img width="4" height="1" alt="">
<a href="https://x.com/intent/user?screen_name=browser_use"><img src="https://media.browser-use.tools/badges/twitter" alt="Twitter"></a>
<img width="4 height="1" alt="">
<a href="https://link.browser-use.com/discord"><img src="https://media.browser-use.tools/badges/discord" alt="Discord"></a>
<img width="4" height="1" alt="">
<a href="https://cloud.browser-use.com"><img src="https://media.browser-use.tools/badges/cloud" height="48" alt="Browser-Use Cloud"></a>
<img width="4" height="1" alt="">
<a href="https://www.producthunt.com/products/browser-use?embed=true&amp;utm_source=badge-featured&amp;utm_medium=badge&amp;utm_campaign=badge-bu" target="_blank" rel="noopener noreferrer"><img alt="BU - Openclaw in the cloud | Product Hunt" width="250" height="54" src="https://api.producthunt.com/widgets/embed-image/v1/featured.svg?post_id=1087770&amp;theme=light&amp;t=1772349539172"></a>
</div>

</br>

🌤️ Want to skip the setup? Use our <b>[cloud](https://cloud.browser-use.com)</b> for faster, scalable, stealth-enabled browser automation!

# 🤖 LLM Quickstart

1. Direct your favorite coding agent (Cursor, Claude Code, etc) to [Agents.md](https://docs.browser-use.com/llms-full.txt)
2. Prompt away!

<br/>

# 👋 Human Quickstart

**1. Create environment and install Browser-Use with [uv](https://docs.astral.sh/uv/) (Python>=3.11):**
```bash
uv init && uv add browser-use && uv sync
# uvx browser-use install  # Run if you don't have Chromium installed
```

**2. [Optional] Get your API key from [Browser Use Cloud](https://cloud.browser-use.com/new-api-key) (new signups get $10 free credits):**
```
# .env
BROWSER_USE_API_KEY=your-key
# GOOGLE_API_KEY=your-key
# ANTHROPIC_API_KEY=your-key
```

**3. Run your first agent:**
```python
from browser_use import Agent, Browser, ChatBrowserUse
# from browser_use import ChatGoogle  # ChatGoogle(model='gemini-3-flash-preview')
# from browser_use import ChatAnthropic  # ChatAnthropic(model='claude-sonnet-4-6')
import asyncio

async def main():
    browser = Browser(
        # use_cloud=True,  # Use a stealth browser on Browser Use Cloud
    )

    agent = Agent(
        task="Find the number of stars of the browser-use repo",
        llm=ChatBrowserUse(),
        # llm=ChatGoogle(model='gemini-3-flash-preview'),
        # llm=ChatAnthropic(model='claude-sonnet-4-6'),
        browser=browser,
    )
    await agent.run()

if __name__ == "__main__":
    asyncio.run(main())
```

Check out the [library docs](https://docs.browser-use.com) and the [cloud docs](https://docs.cloud.browser-use.com) for more!

<br/>

# 🧪 Search-for-Prof Phase 1 (implemented in this repo)

### Phase-1 done contract

- **Input (text-only):** `interests`, `target_institutions`, optional constraints (`degree_level`, `max_professors`, notes/start-term)
- **Output (ranked professors):**
  - recruiting status (`open | closed | unclear`)
  - confidence
  - evidence quote + URL
  - fit score
  - checked timestamp

### Architecture flow (Phase-1)

```text
Input -> discover_faculty_pages -> resolve_professor_sites -> crawl_professor_pages
      -> extract_recruiting_signals + extract_research_areas
      -> compute_fit_score -> rank_results -> persist SQLite + return API/CLI payload
```

The text-input-only Phase 1 pipeline for professor/lab matching and recruiting-status extraction includes:

- New module: `browser_use/professor_search/service.py`
  - `Phase1Input`, `ProfessorCandidate`, `RecruitingEvidence`, `Phase1Result` schemas (`browser_use/professor_search/models.py`)
  - institution seeding (`discover_institution_pages`)
  - faculty/profile URL extraction + URL normalization
  - bounded crawl (depth=2, page cap=10, domain allowlist)
  - retries + timeout + backoff + normalized URL dedupe
  - prioritized crawl links: `prospective|join|openings|positions|phd|intern|students`
  - recruiting evidence extraction (`open | closed | unclear`) with confidence
    - optional LLM extraction when `OPENAI_API_KEY` is set
    - regex fallback for explicit recruiting phrases
  - interest/research matching + fit scoring:
    - `fit_score = 0.6 * interest_similarity + 0.4 * recruiting_confidence_if_open`
    - closed status is penalized
  - SQLite persistence tables:
    - `runs`, `professors`, `pages_visited`, `evidence`, `results`
  - config file: `browser_use/professor_search/phase1_config.yaml` (crawl/scoring/model tuning)
  - robots.txt checks + per-domain rate limiting
- CLI entry script: `run_agent.py`
  - example:
    ```bash
    python run_agent.py \
      --interests "NLP, LLM alignment, information retrieval" \
      --institution "University of X" \
      --degree phd
    ```
  - outputs terminal table + JSON file (`phase1_results.json` by default)
- Optional API path via `run_agent.py:create_app()` with `POST /match` (FastAPI if installed).
  - response contract: `run_id`, `input_echo`, sorted `results[]`
- Environment variables:
  - `OPENAI_API_KEY` (optional; enables LLM-based recruiting extraction)
  - `OPENAI_MODEL` (optional override; defaults to `gpt-4o-mini`)
- Known limitations:
  - no login/form submission in Phase-1
  - focuses on professor/lab/institution domains only
  - keyword-first interest matching (optional embeddings not enabled by default)
- JSON response example:
  ```json
  {
    "run_id": 12,
    "input_echo": {"interests": ["nlp"], "target_institutions": ["CMU"]},
    "results": [
      {
        "name": "Alice Smith",
        "recruiting_status": "open",
        "status_confidence": 0.93,
        "evidence_text": "I am accepting PhD students for Fall 2026.",
        "evidence_url": "https://example.edu/alice",
        "fit_score": 0.88,
        "checked_at": "2026-03-02T15:00:00+00:00"
      }
    ]
  }
  ```
- Focused tests added: `tests/test_professor_search_service.py`

<br/>

# Demos


### 📋 Form-Filling
#### Task = "Fill in this job application with my resume and information."
![Job Application Demo](https://github.com/user-attachments/assets/57865ee6-6004-49d5-b2c2-6dff39ec2ba9)
[Example code ↗](https://github.com/browser-use/browser-use/blob/main/examples/use-cases/apply_to_job.py)


### 🍎 Grocery-Shopping
#### Task = "Put this list of items into my instacart."

https://github.com/user-attachments/assets/a6813fa7-4a7c-40a6-b4aa-382bf88b1850

[Example code ↗](https://github.com/browser-use/browser-use/blob/main/examples/use-cases/buy_groceries.py)


### 💻 Personal-Assistant.
#### Task = "Help me find parts for a custom PC."

https://github.com/user-attachments/assets/ac34f75c-057a-43ef-ad06-5b2c9d42bf06

[Example code ↗](https://github.com/browser-use/browser-use/blob/main/examples/use-cases/pcpartpicker.py)


### 💡See [more examples here ↗](https://docs.browser-use.com/examples) and give us a star!

<br/>

# 🚀 Template Quickstart

**Want to get started even faster?** Generate a ready-to-run template:

```bash
uvx browser-use init --template default
```

This creates a `browser_use_default.py` file with a working example. Available templates:
- `default` - Minimal setup to get started quickly
- `advanced` - All configuration options with detailed comments
- `tools` - Examples of custom tools and extending the agent

You can also specify a custom output path:
```bash
uvx browser-use init --template default --output my_agent.py
```

<br/>

# 💻 CLI

Fast, persistent browser automation from the command line:

```bash
browser-use open https://example.com    # Navigate to URL
browser-use state                       # See clickable elements
browser-use click 5                     # Click element by index
browser-use type "Hello"                # Type text
browser-use screenshot page.png         # Take screenshot
browser-use close                       # Close browser
```

The CLI keeps the browser running between commands for fast iteration. See [CLI docs](browser_use/skill_cli/README.md) for all commands.

### Claude Code Skill

For [Claude Code](https://claude.ai/code), install the skill to enable AI-assisted browser automation:

```bash
mkdir -p ~/.claude/skills/browser-use
curl -o ~/.claude/skills/browser-use/SKILL.md \
  https://raw.githubusercontent.com/browser-use/browser-use/main/skills/browser-use/SKILL.md
```

<br/>

## Integrations, hosting, custom tools, MCP, and more on our [Docs ↗](https://docs.browser-use.com)

<br/>

# FAQ

<details>
<summary><b>What's the best model to use?</b></summary>

We optimized **ChatBrowserUse()** specifically for browser automation tasks. On avg it completes tasks 3-5x faster than other models with SOTA accuracy.

**Pricing (per 1M tokens):**
- Input tokens: $0.20
- Cached input tokens: $0.02
- Output tokens: $2.00

For other LLM providers, see our [supported models documentation](https://docs.browser-use.com/supported-models).
</details>


<details>
<summary><b>Can I use custom tools with the agent?</b></summary>

Yes! You can add custom tools to extend the agent's capabilities:

```python
from browser_use import Tools

tools = Tools()

@tools.action(description='Description of what this tool does.')
def custom_tool(param: str) -> str:
    return f"Result: {param}"

agent = Agent(
    task="Your task",
    llm=llm,
    browser=browser,
    tools=tools,
)
```

</details>

<details>
<summary><b>Can I use this for free?</b></summary>

Yes! Browser-Use is open source and free to use. You only need to choose an LLM provider (like OpenAI, Google, ChatBrowserUse, or run local models with Ollama).
</details>

<details>
<summary><b>Terms of Service</b></summary>

This open-source library is licensed under the MIT License. For Browser Use services & data policy, see our [Terms of Service](https://browser-use.com/legal/terms-of-service) and [Privacy Policy](https://browser-use.com/privacy/).
</details>

<details>
<summary><b>How do I handle authentication?</b></summary>

Check out our authentication examples:
- [Using real browser profiles](https://github.com/browser-use/browser-use/blob/main/examples/browser/real_browser.py) - Reuse your existing Chrome profile with saved logins
- If you want to use temporary accounts with inbox, choose AgentMail
- To sync your auth profile with the remote browser, run `curl -fsSL https://browser-use.com/profile.sh | BROWSER_USE_API_KEY=XXXX sh` (replace XXXX with your API key)

These examples show how to maintain sessions and handle authentication seamlessly.
</details>

<details>
<summary><b>How do I solve CAPTCHAs?</b></summary>

For CAPTCHA handling, you need better browser fingerprinting and proxies. Use [Browser Use Cloud](https://cloud.browser-use.com) which provides stealth browsers designed to avoid detection and CAPTCHA challenges.
</details>

<details>
<summary><b>How do I go into production?</b></summary>

Chrome can consume a lot of memory, and running many agents in parallel can be tricky to manage.

For production use cases, use our [Browser Use Cloud API](https://cloud.browser-use.com) which handles:
- Scalable browser infrastructure
- Memory management
- Proxy rotation
- Stealth browser fingerprinting
- High-performance parallel execution
</details>

<br/>

<div align="center">

**Tell your computer what to do, and it gets it done.**

<img src="https://github.com/user-attachments/assets/06fa3078-8461-4560-b434-445510c1766f" width="400"/>

[![Twitter Follow](https://img.shields.io/twitter/follow/Magnus?style=social)](https://x.com/intent/user?screen_name=mamagnus00)
&emsp;&emsp;&emsp;
[![Twitter Follow](https://img.shields.io/twitter/follow/Gregor?style=social)](https://x.com/intent/user?screen_name=gregpr07)

</div>

<div align="center"> Made with ❤️ in Zurich and San Francisco </div>
