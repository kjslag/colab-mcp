---
name: colab-notebooks
description: This skill should be used when the user asks to work in a Google Colab notebook, such as "open my Colab notebook", "add a cell in Colab", "run this in Colab", "look at my notebook", or pastes a colab.research.google.com link.
---

# Working in a Colab notebook

The `colab` MCP server bridges to a Colab tab in the user's own browser. The notebook tools are served by that tab, so they exist only while a tab is connected.

## Connect

1. Call `open_colab_browser_connection`. Pass `notebook_url` when the user names a notebook (a `https://colab.research.google.com/...` link); omit it for a new scratch notebook.
2. Tell the user a Colab tab is opening and that they need to click **Connect** in it (and allow local network access if Chrome asks). The call waits up to 60 seconds.
3. `true` means connected. `false` means it timed out: ask the user whether the tab opened and they clicked Connect, then call again.
4. If the call errors with "already connected", a tab is connected but this server cannot tell which notebook it shows. Use `get_cells` to see what is there, or ask the user to close that tab before connecting to a different notebook.

## Use the notebook tools

After connecting, the tab's tools (for example `get_cells`, `add_code_cell`, `update_cell`, `run_code_cell`) normally appear in the tool list. If they do not, call `list_notebook_tools` for their names and schemas, then call each one through `call_notebook_tool` with `name` and `arguments`. Never guess a tool's arguments; read its schema first.

## Work carefully

- Read the notebook with `get_cells` before changing it, and again after running cells, so edits are based on its current state rather than memory.
- The notebook belongs to the user and is open in front of them. Add or edit only what the task needs; do not delete or rewrite cells the user did not ask about.
- Running a cell executes code in the user's Colab runtime under their Google account. Run only what the task calls for, and say what you ran.
- If a call reports that no tab is connected, the user closed the tab or the connection dropped: connect again rather than retrying the call.
