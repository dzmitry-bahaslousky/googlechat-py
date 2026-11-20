# googlechat-py

Use Google Chat from Python, the command line, or an AI agent — signed in with your own browser session. No Google Cloud project, no OAuth app, no admin rights.

Three ways to use it:

- **`googlechat-mcp`** — an MCP server, so Claude (or another agent) can read and send your chats
- **`gchat`** — a command-line tool
- **`googlechat`** — an async Python library

> ⚠️ This uses Google Chat's private web API, the one chat.google.com itself uses. It is not official and can break whenever Google changes it. Check that your organization allows this before relying on it.

## Install

You need [`uv`](https://docs.astral.sh/uv/) and Google Chrome (Chrome is used only to sign in).

```bash
uv tool install git+https://github.com/dzmitry-bahaslousky/googlechat-py
```

## Sign in

```bash
googlechat-mcp login
```

Chrome opens chat.google.com. Sign in as usual; the window closes by itself. Run the same command again when the session expires — it first tries to refresh silently. `googlechat-mcp logout` removes the saved login.

## Use with Claude Code

```bash
claude mcp add --scope user googlechat -- googlechat-mcp
```

Claude can then list chats (including which have unread messages), and read and search messages. That's all by default.

To also let Claude send, reply, react, and edit or delete your own messages, turn on write tools:

```bash
claude mcp add --scope user -e GCHAT_MCP_WRITE=1 googlechat -- googlechat-mcp
```

> ⚠️ Messages Claude reads are written by other people. A message can try to trick Claude into posting or deleting something as you. With write tools on, keep Claude Code's approval prompts on for them.

## Use from the command line

```bash
gchat spaces -n 10                    # your chats
gchat sections one                    # chats in one of your sidebar sections
gchat unread                          # chats with messages you haven't read
gchat read SPACE_ID -n 5              # latest threads in a chat
gchat thread SPACE_ID THREAD_ID       # one whole thread
gchat search "release"                # search all chats
gchat send SPACE_ID "Hello"           # asks before posting
gchat reply SPACE_ID THREAD_ID "On it"
gchat --json spaces                   # JSON output for scripts
```

Run `gchat --help` for all commands. Commands that post or change something as you ask for confirmation first; in scripts, add `--yes`.

## Use from Python

```python
import asyncio
from googlechat import GoogleChatClient

async def main():
    async with GoogleChatClient.from_storage() as chat:
        spaces = await chat.spaces.list(page_size=10)
        for m in await chat.messages.list(spaces[0].id):
            print(m.sender.name, m.text)

asyncio.run(main())
```

Available: `chat.spaces`, `chat.messages`, `chat.members`, `chat.reactions`, `chat.users`.

## Settings

| Environment variable | Default |
|---|---|
| `GCHAT_CONFIG_DIR` | `~/.config/googlechat-mcp` |
| `GCHAT_COOKIES` | `$GCHAT_CONFIG_DIR/cookies.json` |
| `GCHAT_PROFILE` | `$GCHAT_CONFIG_DIR/browser-profile` |
| `GCHAT_MCP_WRITE` | off; `1` turns on the MCP server's write tools |

## Troubleshooting

| Problem | Fix |
|---|---|
| `Not signed in` or `Session expired` | Run `googlechat-mcp login` |
| Errors on one feature only | Google probably changed the API — see [purple-googlechat](https://github.com/EionRobb/purple-googlechat) |

## Development

```bash
uv sync
uv run pytest      # offline tests, no account needed
```

## Credits

Protocol from [EionRobb/purple-googlechat](https://github.com/EionRobb/purple-googlechat).
