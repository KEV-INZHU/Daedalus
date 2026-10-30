# Coordination: agents talking over the shared bus

A run's agents can coordinate over the shared, local filesystem message bus: post decisions,
ask and answer questions, reply in threads, and pick up the context when they join late or restart.
The bus carries messages; it decides nothing. Ariadne and her audit log stay authoritative for the
lifecycle, reviews and acceptance. A message on the bus never changes run state.

Code: `daedalus.coordination` (`bus.py`, `channel.py`, `identity.py`, `surface.py`) and the
`daedalus bus` command.

**This is a library API that callers opt into.** Daedalus's own launch paths (`daedalus run`,
launched reviews) do not yet mint names or attach the channel. An orchestrator that wants
coordination calls `Channel.spawn` for each subagent before it works, and `surface.attach` on the
request before handing it to the adapter.

## Layout

`FileBus` reads and writes the shared bus's layout, so it can point at the same directory:

```
<bus>/topics/daedalus.jsonl   canonical history (append-only)
<bus>/inbox/<agent>.jsonl     delivery copies, one per subscriber
<bus>/subscribers.json        {agent: {topics, filters, subscribed_at, expires_at}}
<bus>/cursors/<agent>.json    Daedalus: each agent's read position, seen ids, held messages
<bus>/receipts/<agent>.jsonl  Daedalus: acknowledgements ("acted")
```

A message has `id`, `ts`, `sender`, `topic`, `type`, `priority`, `status`, `subject`, `body`,
`refs` and `reply_to`. Daedalus adds `run_id`, `task_id` and `role`. Inbox file names replace
characters Windows rejects (`:`) and add a short hash, so two names never share a file.

The bus location is `$DAEDALUS_BUS_DIR`, else `.daedalus/bus` in the repository.

**To use the shared bus itself**, set `DAEDALUS_BUS_DIR` to its root and add `daedalus` to its
fixed topic list in `bus.py`. Topics are fixed on both sides: posting or subscribing to an unlisted
topic fails instead of creating one.

Daedalus writers serialize on an OS lock (`flock`, or `msvcrt.locking` on Windows) held on
`<bus>/.lock`. The lock file stays in place, and a crashed writer's lock is released with its
process, so no stale lock ever has to be broken. **`bus.py` does not take that lock**, so when both
write the same bus:

- `subscribers.json` is read, changed and replaced whole by each side. Two concurrent subscription
  changes can lose one of them (a lost update). Subscribe Daedalus agents when `bus.py` is not
  changing subscriptions, or make `bus.py` take the same lock.
- Appends from the two sides can interleave. Each Daedalus message is one `write` of one line, so
  lines stay whole on local POSIX filesystems. Network drives and synced folders (OneDrive, for
  example) make no such promise. A torn or foreign line is skipped, never fatal.
- A malformed `subscribers.json` is an error, never read as empty, so Daedalus will not overwrite
  every subscription with its own.

Lines are only read if they have the fields, with the types, that the code relies on (`id`,
`sender`, `topic`, `type` and `subject` strings, a numeric `ts`, a list of string `refs`). Anything
else is skipped in history and inboxes alike.

## Identity

Each subagent gets a generated name, `agent:<role>:<uuid4>` (`Channel.spawn` or
`coordination.new_agent_name`). Ariadne records it in the audit log (`agent.registered`) against
the run, task, role and **principal** it stands for. Registration is a no-op when an agent resumes
with the same binding. It is refused when it would:

- move a name to another principal, task or role;
- bind a name to a human or system principal;
- chain aliases (use an alias as a principal, or turn a principal into an alias);
- register a name that has already authored a candidate (that would re-point its authorship);
- revive a retired name.

A name is for attribution, not proof of identity. The bus does not authenticate senders. A
process that can write the bus files can post as anyone, and the channel cannot stop that. What
the channel does check is that the sender is a registered, unretired agent of this run.

## The channel

`Channel(ari, run_id, bus)` serves one run. Each subscription filters on `run_id`, so runs that
share a bus never see each other's messages; they are not even delivered.

| Call | What it does |
|------|--------------|
| `join(name)` | Subscribe (keeping the original start on a resubscribe), reconcile, and return a `Bootstrap`: objective, acceptance criteria, decision threads, task ownership, open blockers, artifact refs, the visible history, and `new` (what this agent had not seen). |
| `post(name, type, subject, ...)` | Publish `note`, `question`, `answer`, `decision`, `blocker`, `artifact`, `status` or `finding`. `reply_to` must be a message of this run. |
| `read(name)` | Reconcile the inbox against the topic log, then return messages not yet seen, oldest first. |
| `ack(name, id)` | Record that the agent acted on a message it has seen. |
| `receipts(id)` | Each agent's stage for a message. |
| `retire(name)` / `expire()` | End an agent and drop its subscription. `expire` also drops subscriptions to this run held by unregistered or retired agents, all of them once the run has finished, and any past their `ttl_s`. |

Subscribing comes before reading history, so a message posted while an agent joins is in one or
the other. Deduplication by message id means it is handed over once.

### Receipt stages

| Stage | Meaning |
|-------|---------|
| published | in the topic log |
| delivered | a copy is in the agent's inbox |
| seen | the channel handed it over (`read`, `join`, or a launched session's prompt) |
| acted | the agent acknowledged it |

A watcher noticing an inbox change proves only *delivered*. Posting appends to history before fan-out,
so a failed inbox write leaves a message *published* but not *delivered*. `post` names the
subscribers it missed (`undelivered`), and `read`, `join` and `daedalus bus reconcile` repair
inboxes from history. Reconciling is idempotent. It covers what was posted since the agent first
subscribed, and `join` covers anything earlier.

## Harness integration

A filesystem watcher cannot make a running model read anything. What Daedalus controls is the
session it launches. `coordination.surface.attach(request, channel, name)` joins the agent, puts
the bootstrap and every unread message into the request's prompt, and marks them *seen*. What
agents wrote goes in one JSON block, labelled as peer input rather than instructions from
Daedalus. Newlines, backticks and non-ASCII characters inside it are escaped, so a message cannot
close the block or pass for Daedalus's own text. `daedalus bus read` prints one JSON object per
message for the same reason. It also
sets `DAEDALUS_BUS_DIR`, `DAEDALUS_RUN_ID` and `DAEDALUS_AGENT_NAME` in `AgentRequest.env`. The
command adapter passes `env` to the session (and always re-marks it `DAEDALUS_AGENT=1`). From
inside the session, the agent uses:

```
daedalus bus read
daedalus bus post decision "use JSON over the wire" --ref docs/api.md
daedalus bus post answer "agreed" --reply-to msg-...
daedalus bus ack msg-...
```

`daedalus bus` is not a human-authority command, so it works inside agent sessions. Adapters that
send only a prompt (the OpenAI-compatible adapter, for example) get the context through the
prompt, but they cannot run `daedalus bus` mid-session.

## Review independence

- Builders share coordination context freely, including reviewers' `finding` messages.
- A reviewer (a role in `socrates`, `mozi`, `aristotle`, `james` or `reviewer`) is not handed
  another agent's `finding` messages until it has submitted its own review **of the candidate the
  finding is about** (each finding records the candidate current when it was posted). They are
  held, not dropped: the first `read` after that review is recorded releases them. History and
  bootstrap hide them too.
- A registered agent whose role is not a reviewer role cannot submit a review. The channel showed
  it every finding, so its review would not be first-pass.
- Authorship follows the principal, not the name. `submit_review`, `record_arbitration`, the
  acceptance check and review reuse all ignore a review by any name whose registered principal
  (or any alias of it) authored the candidate, so a fresh random alias does not make a builder an
  independent reviewer of its own work. A name cannot be registered after it has authored,
  reviewed or arbitrated, since that would re-point its earlier work at another principal.
- Only reviews Daedalus launched itself, with no recorded exposure, count as independent for
  acceptance (unchanged). A review submitted by hand never qualifies, whatever name it uses.

The sealing is enforced by the channel. An agent that reads the bus files directly bypasses it,
like any other process with file access (see [security-model.md](security-model.md)).

## Limits

- No authentication or signing: the separate identity and authorization design for the bus
  (signed identities, a broker) is a proposal, not something this uses.
- Reading through the channel is what the sealing and receipts see. A process that reads the bus
  files directly is not recorded as exposed to anything.
- No push into a session that is already running. New messages reach it when it runs
  `daedalus bus read`, or at its next launch.
- `ack` is the agent's own claim. It is not evidence for acceptance.
