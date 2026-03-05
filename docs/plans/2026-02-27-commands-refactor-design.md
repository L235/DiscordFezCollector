# Commands Refactor & Test Foundation Design

## Problem

`commands.py` is 800 lines with significant duplication:
- 8 near-identical filter mutation commands (track/ignore/untrack/unignore x thread/receiver)
- Duplicated config CRUD in `config.py` (thread vs receiver variants)
- Manually maintained help text that's already broken (#23, 2000 char limit)
- Import-time side effects (`sys.exit`, bot creation) block testability
- No meaningful test coverage to support future refactoring

The design must also accommodate issue #18 (multi-channel operation), where `DISCORD_CHANNEL_ID` becomes a set of parent channels across servers.

## Approach

Shared helpers with flat command files. No Cogs — the bot is single-owner and doesn't benefit from Cog ceremony.

## Config Layer Unification

### Unified entity operations

Collapse duplicated thread/receiver config functions into generic ones parameterized by `entity_type` (`"threads"` or `"receivers"`):

```python
async def get_entity_entry(entity_type: str, entity_id: str) -> Optional[dict]
async def mutate_entity_config_list(entity_type: str, entity_id: str, field: str, *, add=None, remove=None, clear=False) -> Tuple[bool, Optional[List[str]]]
async def update_entity_config(entity_type: str, entity_id: str, new_cfg: dict) -> bool
```

Functions with genuinely different logic stay specific: `ensure_custom_thread_entry`, `create_receiver`, `delete_receiver`, `set_receiver_active`.

### Import-time side effects

Move `sys.exit()` behind a `validate_env()` function called from `main()`. Config module exports env vars but doesn't crash on import. This makes all pure logic importable in tests without env var hacks.

## Commands Layer

### File structure

```
src/commands/
  __init__.py            # imports submodules to register commands
  helpers.py             # authorised(), mutate_filter(), _require_custom_thread(), context checks
  thread_commands.py     # /new thread, /new userthread, /activate, /deactivate, legacy aliases
  config_commands.py     # /config (get/set/add/remove/clear/getraw/setraw), /globalconfig
  receiver_commands.py   # /receiver (list/add/remove/activate/deactivate/config/track/ignore/...)
  filter_commands.py     # /track, /ignore, /untrack, /unignore (thread-context shortcuts)
  help_commands.py       # /fezhelp (auto-generated)
```

### Filter deduplication

One shared helper handles all 8 filter commands:

```python
async def mutate_filter(
    ctx, entity_type: str, entity_id: str,
    target: FilterType, value: str,
    *, action: str  # "add_include", "remove_include", "add_exclude", "remove_exclude"
) -> None
```

Command functions become ~3 lines each: validate context, call `mutate_filter`.

### Context checking (#18-ready)

`_in_parent_channel(ctx)` and `_require_custom_thread(ctx)` move to `helpers.py` and accept a set of channel IDs rather than one global. Currently a set of one, but shaped for #18 where it becomes multi-channel. When #18 lands, only `helpers.py` changes.

### Auto-generated help

`help_commands.py` builds help text dynamically from registered command metadata (the `description` strings already on every decorator). Groups by command group, formats into Discord markdown, splits at section boundaries to guarantee each message is under 2000 chars. Closes #23.

Legacy aliases (`!add`, `!addcustom`) are `hidden=True` and excluded from generated output.

## Test Strategy

pytest, focused on pure logic. Comprehensive enough to be the safety net for red-green-refactor.

### Config layer tests (`tests/test_config.py`)

- `mutate_entity_config_list`: add, remove, clear, deduplication, unknown entity, concurrent mutations
- `update_entity_config`: replace, missing entity
- `_normalise_list_val`: string, list, edge cases
- `parse_webhooks_env`: valid, invalid, empty
- Persistence: write -> reload -> verify
- Error paths: corrupt file, missing keys

### Filter/model tests (`tests/test_models.py`)

- `CustomFilter.matches()`: full matrix of filter type x include/exclude x match/no-match
- Combined filters, case sensitivity, empty patterns, malformed regex
- `_compile_pattern_list`: edge cases

### Command helper tests (`tests/test_command_helpers.py`)

- `_parse_json_arg`: JSON vs fallback to string
- `_build_help_sections`: all chunks under 2000 chars
- Context-checking logic with various channel types (key for #18)
- `mutate_filter`: minimal ctx mock, verify correct config function called

## Execution Order

Each step is independently deployable:

1. Fix import-time side effects
2. Unify config.py (foundation layer)
3. Write config + model tests (lock down foundation)
4. Split commands.py + deduplicate (big refactor, with tests as safety net)
5. Write command helper tests
6. Auto-generate help
7. Clean up dead code, verify everything passes
