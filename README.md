<p align="center">
  <img src="assets/logo.png" alt="Tactile logo" width="160">
</p>

<p align="center">
  <strong>English</strong> · <a href="README_zh.md">简体中文</a>
</p>

# Tactile

**An accessibility-first operating layer for agents.**

> Stop guessing pixels. Start touching semantics.

Tactile is not another computer-use agent. It is a skill, protocol, and tool layer that helps agents operate software through accessibility semantics first.

When an agent needs to use an application, Tactile asks it not to begin with screenshots, guessed coordinates, and pixel-level clicks. Instead, it should first inspect the semantic information already exposed by the operating system and the application:

- What role does this element have?
- Does it have an accessible name?
- Is it clickable, selected, focused, enabled, or disabled?
- Where does it sit in the UI hierarchy?
- Does it expose an action that can be invoked directly?

In that sense, Tactile gives agents a way to feel the structure of software before reaching for vision.

This information already exists for screen readers and assistive technologies. Tactile makes it the first entry point for agents as well. The easier software is for Tactile to operate, the more likely it is to support genuinely accessible interaction for humans as well.

**Agent-ready software should also be human-accessible software.**


## Demo

**Tactile gives agents a sense of touch.**

### Lark and WeChat workflow

This demo video was also edited by an agent using the Tactile skill to operate CapCut.

https://github.com/user-attachments/assets/49dc6bfe-0661-4ab0-9099-be3849b4137a

### CapCut video-editing workflow

https://github.com/user-attachments/assets/7bc0f05e-9228-4cf1-abe3-ffb7e4722be2


## How to Use Tactile

### Prefer the macOS MCP

For macOS, you can use the dedicated Tactile MCP in
`mcps/tactile-macos-mcp`. It is the recommended entry point when available:
it is faster, easier to use, and exposes Tactile's accessibility-first workflow
directly through MCP tools.

Use the MCP server with:

```bash
mcps/tactile-macos-mcp/bin/tactile-macos-mcp
```

### Use Tactile as a skill

Ask your agent to configure this skill from the repository:

```txt
Configure this skill for me (make sure to choose the version for the corresponding operating system): https://github.com/yliust/Tactile
```

If using API:

```txt
export TACTILE_OPENAI_BASE_URL=xxxxxxx
export TACTILE_OPENAI_API_KEY=xxxxxxx
export TACTILE_MODEL='gpt-5.5'
```


## Why Tactile?

Many computer-use agents start from screenshots:

```txt
look at screenshot -> infer element -> predict coordinates -> click -> inspect screenshot again
```

This approach is general, but fragile. Tactile changes the order of operations:

```txt
read accessibility semantics -> use OCR-grounded coordinates when needed -> fall back to visual computer use
```

Agents should not only see software on a screen. When better information is available, they should first touch the structure of the interface.


## Tactile v0

Tactile v0 will begin as a skill.

Its goal is to package an accessibility-first operating method for agents:

1. **Use accessibility semantics first**

   If the system or application exposes useful accessibility information, the agent should use element roles, names, hierarchy, state, and actions to understand and operate the interface.

2. **Use OCR + coordinates when semantics are incomplete**

   If an element is not fully represented in the accessibility layer but the visible text is readable, the agent can use system OCR. System OCR usually returns both text and coordinates, which makes it a text-grounded fallback rather than pure visual guessing. For clear text buttons and labels, this can reduce token usage, retries, and time.

3. **Fall back to the agent's native visual operating logic**

   If the accessibility layer is unavailable, OCR cannot locate the target, or the current interface is canvas-based, game-like, remote, image-heavy, or otherwise semantically opaque, the agent can fall back to its own runtime or tool-specific visual operating logic.

Tactile provides operating strategy and method tools. It does not take over all agent decisions. When to downgrade, retry, or hand control back to the agent remains context-dependent.


## Workflow

Tactile recommends the following operating ladder:

```txt
Level 1: Accessibility semantics
  Read the accessibility tree
  Operate through element names, roles, states, hierarchy, and actions
  Best for standard UI such as buttons, text fields, menus, tables, dialogs, and lists

Level 2: OCR-grounded coordinates
  Use system OCR to read visible text and its coordinates
  Use text locations to click, type, and verify
  Best for interfaces with incomplete accessibility metadata but readable text

Level 3: Native visual computer use
  Use the agent's existing screenshot understanding, visual reasoning, and coordinate actions
  Best for image-based interfaces or environments with little usable semantic structure
```

Humans and agents can move faster when they can share the same semantic path through software.


## Verification

Tactile is concerned not only with where an agent clicked, but also with whether the task actually succeeded.

After each operation, the agent should verify the result whenever possible:

1. **Prefer accessibility-state verification**

   For example: whether a button became disabled, a checkbox became selected, a text field value changed, a dialog closed, or a new list item appeared.

2. **Use OCR verification when accessibility state is insufficient**

   If visible text changes, the agent can use OCR to check whether the expected text, error message, success state, or page title appeared.

3. **Use screenshot-based visual verification as the final fallback**

   When semantics and OCR are not enough, the agent can use screenshot understanding and visual reasoning to confirm the result.

Verification failure does not always mean the action failed. It means the interface did not provide enough reliable feedback, and the agent may need to retry, choose another path, or fall back to a more general visual operating method.


## Why Build This Ecosystem?

Many attempts to make agents better require new agent-friendly interfaces. Tactile asks a different question: is there an interface that can serve both humans and agents?

We have found that when agents use accessibility entry points, they can operate more reliably. At the same time, if agents begin to depend on accessibility, long-standing accessibility gaps become easier to notice:

- Buttons without readable names
- Incorrect control roles
- Dialogs that are invisible to the accessibility tree
- State changes that are not exposed to assistive technologies
- Custom components that are visible only to sighted users
- Incomplete keyboard and screen reader paths

These problems affect agents, but they also affect real users, especially people who depend on screen readers, keyboard navigation, and assistive technologies.

Tactile's long-term goal is not only to help agents operate computers better.

It also aims to encourage software ecosystems to expose better semantic structure, so that agent-ready software can also become accessible software for all humans.


## Current Status

Tactile is still early.

The first version connects to Codex as a skill. In early tests on macOS applications with reasonable accessibility support, the accessibility-first workflow can significantly reduce screenshot reasoning and coordinate retries, though of course it is not universal. As execution experience is distilled into reusable strategies, examples, and tool constraints, the skill can continue to improve task outcomes; this kind of experience reuse has proven valuable across many forms of automation work.

We are also seeing that even many widely used applications still lack strong accessibility support. At the same time, developers are already being asked to adapt to a growing number of agent-specific interfaces. Tactile explores whether these paths can converge.

A longer-term goal is to provide interface layers for software that has not yet implemented sufficient accessibility support for human and AI.


## Acknowledgements

Tactile is built on decades of work from accessibility communities, screen readers, assistive technologies, operating-system accessibility APIs, OCR systems, UI automation projects, agent runtimes, and open-source developers.

We are grateful to everyone who has helped make software more readable, operable, and adaptable. Tactile hopes to connect that work with the agent era, and to make the same semantic infrastructure useful to both humans and AI.


## Join Us

If you care about Agentic AI, desktop automation, operating systems, accessibility technology, or simply believe software should be easier for both agents and humans to use, you are welcome to join Tactile.

**Accessible to humans. Operable by agents.**
