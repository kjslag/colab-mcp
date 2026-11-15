# Colab-mcp

An MCP server for bridging your local agent to a Colab session in the browser.

> **This is a fork of [googlecolab/colab-mcp](https://github.com/googlecolab/colab-mcp)**
> with three fixes and Claude Code plugin packaging:
>
> - `open_colab_browser_connection` takes an optional `notebook_url`, so it can
>   connect to an existing notebook instead of always opening a scratch one.
> - Two tools, `list_notebook_tools` and `call_notebook_tool`, are listed from
>   startup and reach the Colab tab's notebook tools by name. Clients that do not
>   act on `notifications/tools/list_changed` can still use the notebook.
> - The local WebSocket binds `127.0.0.1` only. Binding `localhost` can open two
>   sockets on different ports while the Colab tab is told just one.

# Claude Code plugin

Requires [`uv`](https://docs.astral.sh/uv/) on your `PATH`. In Claude Code:

```
/plugin marketplace add kjslag/colab-mcp
/plugin install colab@kjslag-colab
```

Then ask Claude to open a notebook, for example "open
https://colab.research.google.com/drive/<id> and add a cell that prints 2+2".
A Colab tab opens; click **Connect** in it within 60 seconds (and allow local
network access if Chrome asks). The plugin also adds a `colab-notebooks` skill
telling Claude how to work in the notebook.

The first start installs the server's dependencies into the plugin's data
directory, which can take a minute.

# Supported Clients
The notebook tools appear in the tool list after a tab connects, via `notifications/tools/list_changed`.
Clients that ignore that notification can use `list_notebook_tools` and `call_notebook_tool` instead.
The client must run locally on the same machine as the browser.

Popular clients include:
- Gemini CLI
- Claude Code
- Windsurf


# Setup

- Install `uv` (`pip install uv`)
- Configure for usage (eg for mcp.json style services):

```
...
  "mcpServers": {
    "colab-mcp": {
      "command": "uvx",
      "args": ["git+https://github.com/kjslag/colab-mcp"]
    }
  }
...
```

(If you have a non-standard default package index (**Googlers**), you may also need to add `--index https://pypi.org/simple`)

# Issues & Discussions

We are using GitHub [discussions](https://github.com/googlecolab/colab-mcp/discussions) as the
place for issue discussion and feature requests. As discussions mature into action items, we
will add those items as issues. This helps us ensure that issues in the issue tracker are
well-understood, deduplicated, and actionable. For these reasons, **please do <u>NOT</u> open
issues directly.** 

# Contributing 
We unfortunately don't have the bandwidth to support review of external contributions, and we 
don't want user PRs to languish, so we aren't accepting any external contributions right now.

If you have a great idea or pain point, we would love to hear about it on our 
[discussions](https://github.com/googlecolab/colab-mcp/discussions) page - the preferred place 
for issue discussion and feature requests.

# Internal - For Colab Developers

### Prerequisites

- `uv` is required (`pip install uv`)
- Configure git hooks to run repo presubmits

```shell
git config core.hooksPath .githooks
```

### Gemini CLI setup

```
...
  "mcpServers": {
    "colab-mcp": {
      "command": "uv",
      "args": ["run", "colab-mcp"],
      "cwd": "/path/to/github/colab-mcp",
      "timeout": 30000
    }
  }
...
```
