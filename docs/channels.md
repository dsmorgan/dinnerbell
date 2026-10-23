# Channel adapters

This is the contract between Dinnerbell's core and a channel adapter such as Slack. The core decides what happens and records it as typed notifications in the outbox; the adapter only renders and delivers them, and turns button taps back into service calls. Spec sections 9, 13.2, and 13.4 are the background.

## How notifications flow

1. A use case in `services` changes a call and, in the same transaction, writes one outbox row per notification (`db/outbox.py`, table `outbox`). A Slack outage never loses a change.
2. The drainer, `services.outbox.drain`, runs every 5 seconds and after every 30-second scheduler tick. It claims a row, commits the claim, calls the channel with no transaction open, and records the outcome.
3. The channel is wired once in `app_factory.py` as `JobContext.channel`. It is `channels.base.NullChannel` until Slack replaces it. `NullChannel` logs each notification, reports it delivered, and returns made-up message references, so updates and threads run the same path a real channel takes.

## The interface

`Channel` is defined in `domain/notifications.py`, so `services` can use it without importing `channels`, and `channels/base.py` re-exports it. Adapters implement it structurally; there's nothing to subclass.

```python
class Channel(Protocol):
    async def deliver(self, notification: Notification, *, thread: MessageRef | None = None) -> DeliveryResult: ...
    async def update(self, ref: MessageRef, notification: Notification) -> None: ...
```

- `deliver` posts a new message: to the household channel, or as a direct message to `notification.recipient_id` when that isn't `None`. Return `DeliveryResult(MessageRef(channel_id, message_id))` (for Slack, the channel ID and `ts`) for any post that has a `message_key`, because later updates and threads need it. For other posts the ref is optional.
- `thread` is the message to thread the post under. The drainer passes it when the notification has a `thread_key` and that message was delivered; otherwise it's `None` and you post at the top level.
- `update` edits the message at `ref` to show the notification's state.
- Raise on failure. The drainer records `"<ExceptionType>: <message>"` in `outbox.last_error`, so never put a token in an exception message.
- Raise `domain.notifications.PermanentDeliveryError` when retrying can't help, such as a deleted channel or a member who left the workspace. The row is marked failed at once, after one attempt, and stops holding back the event's later rows. Raise anything else for failures worth retrying, such as timeouts or rate limits.

## Delivery guarantees

- **Order.** Rows are delivered oldest first per event. While an event's earlier post is waiting to retry or is claimed by another drainer, its later rows wait too, so an update never arrives before the post it edits. A failed update is the exception: it retries on its own schedule without holding back later rows, because the next update carries the latest state anyway, and any newer update to that message coalesces into it while it waits. Different events don't block each other.
- **Once.** A row is claimed with a conditional `UPDATE` before the channel is called, so concurrent drainers never deliver the same row. The claim lasts 5 minutes (`LEASE`). If the process dies mid-delivery, another drainer can redeliver after that, so treat `idempotency_key` as a dedupe key if a duplicate post would matter.
- **Retries.** A failure is retried after 30 seconds, doubling up to an hour. After 8 attempts (`MAX_ATTEMPTS`), or at once for `PermanentDeliveryError`, the row is marked `failed_at` and stops holding back later rows. `/healthz` reports `outbox.backlog` (waiting) and `outbox.failed`.
- **Updates without a message.** If the post an update edits never landed (it failed for good, or the channel returned no ref), the update is closed out with `last_error = "no message to update"` instead of being sent.

## Keys

Every key starts with the event and its run: `event:{event_id}:run{run}`. An admin re-run increments `run`, so a re-run posts a fresh call message and its own thread.

| Key | Meaning |
|---|---|
| `idempotency_key` | Unique per household. Writing the same key again is ignored, so every transition can be retried safely. |
| `message_key` | Names a message that later notifications edit. Posts with one create it; updates (`is_update`) edit it. Dinnerbell stores the ref the post returned (`outbox.ref_channel`, `outbox.ref_message`), so the adapter needs no table for message refs. |
| `thread_key` | The `message_key` of the message to thread under. |

Updates coalesce: while an update to a message is waiting and unclaimed, a newer update to the same message replaces its payload instead of queueing behind it. Ten quick votes become one edit with the latest counts. Each update row's `idempotency_key` is the class key plus a sequence number, such as `event:7:run1:call_updated:3`.

## Notifications

All types are frozen dataclasses in `domain/notifications.py` with an `event: EventInfo`. They carry only domain data: IDs, display names, the event's state and `end_reason`, UTC times plus the household's `timezone`, items with URLs, and the event page's `path` (join it to `DINNERBELL_BASE_URL` for links, per FR-37). Rows store them as JSON; `from_payload(kind, payload)` rebuilds them. The drainer hands adapters the dataclass.

| `kind` | Class | Audience | `message_key` | `thread_key` | Sent when |
|---|---|---|---|---|---|
| `call_opened` | `CallOpened` | Channel; mention everyone in `mentions` | `…:call` | | A call opens (scheduled, ad hoc, or re-run). |
| `call_updated` | `CallUpdated` | Update | `…:call` | | The roster, coordinator, a deadline, or the state changes. |
| `response_reminder` | `ResponseReminder` | DM `member` | | | Reminder time, once per run, to active members who haven't answered and haven't muted reminders. |
| `maybe_resolved` | `MaybeResolved` | DM `member` | | | The response deadline turned their maybe into out (FR-9). |
| `proposals_ready` | `ProposalsReady` | Channel | `…:proposals` | `…:call` | Responses close and voting opens. |
| `proposals_updated` | `ProposalsUpdated` | Update | `…:proposals` | | Votes, an added proposal, or voting closes. |
| `decision_made` | `DecisionMade` | Channel | `…:decision` | `…:call` | Locked (`locked_by` set) or decided by the vote (`locked_by` is `None`). |
| `decision_updated` | `DecisionUpdated` | Update | `…:decision` | | Meal time, assignments, coordinator, or done. |
| `assignment_made` | `AssignmentMade` | DM `member` | | | Each assignment at the decision, and each later reassignment while decided (FR-20). |
| `call_abandoned` | `CallAbandoned` | Channel | | | Too few people (`reason = too_few`) or nothing covered (`no_coverage`). Never sent for `missed`. |
| `event_cancelled` | `EventCancelled` | Channel; for `participants` | | `…:call` | An admin cancels. |

A call that got too late to finish after an outage ends with `EventInfo.state = abandoned` and `EventInfo.end_reason = missed`, quietly: Dinnerbell sends no `call_abandoned` post and no direct messages, only `call_updated` (and `proposals_updated` if it was voting) so its existing messages show that it ended. A scheduled call that expires this way was never posted and sends nothing. Render `missed` as a quiet "too late to decide" state, not as a new announcement.

Building blocks: `PersonInfo(id, name)`, `RosterEntry` (status, guests, guests' dietary needs, eat window, offered roles, suggestions, note), `ProposalInfo` (rank, `ItemInfo`, mode, reasons, source, proposer, voters; votes are visible, per FR-14), `DecisionInfo` (proposal, meal time, head count, participants with guests, assignments, guests' dietary notes), and `AssignmentInfo(role, member)`.

Notifications never say who vetoed what. Proposal reasons come from combined preferences only.

Members are identified by ID. Resolve IDs to Slack users from `member.slack_user_id`. A lookup by Slack user ID doesn't exist yet; add one to `MemberRepository` with a service function for it, because `channels` may not import `db`.

## Interactions

Map every button or modal to one service call. Services check every rule and permission, scope every ID to the actor's household, write the outbox and audit rows, and commit. The adapter never writes to the database or the outbox itself.

1. Resolve the Slack user to an active member of the household, or refuse with an ephemeral message (spec 13.5).
2. Open a session from the app's `sessionmaker` and call the service with `now=clock()`.
3. Show these exceptions as ephemeral messages with `str(exc)`: `EventRuleViolation`, `InvalidResponse`, `services.errors.NotFound`, and `services.errors.Forbidden`. Treat `IllegalTransition` as "That can't happen anymore." Anything else is a bug: log it.
4. Don't update messages from the interaction handler. The service's notifications do that through the outbox within seconds.

| Slack action | Service call |
|---|---|
| In, Out, Maybe | `calls.respond(db, actor, event_id, Status.IN / OUT / MAYBE, now=)` |
| I can cook, I can pick up, I'll order (toggle) | `calls.offer_role(db, actor, event_id, RoleOffer.COOK / PICKUP / ORDER, offered, now=)` |
| I'll coordinate | `calls.claim_coordinator(db, actor, event_id, now=)` |
| Details modal | `calls.save_details(db, actor, event_id, Details(guests=, guest_needs=, eat_from=, eat_until=, suggestion_item_ids=, suggestion_texts=, note=), now=)` |
| Vote, or vote again to remove | `calls.vote` and `calls.unvote(db, actor, event_id, proposal_id, now=)` |
| Lock (coordinator or admin) | `calls.lock(db, actor, event_id, proposal_id, now=)` |
| Add proposal modal | `calls.add_proposal(db, actor, event_id, mode, item_id=, free_text=, cuisine_id=, now=)` |
| Mark done | `calls.mark_done(db, actor, event_id, now=)` |
| Hand off | `calls.hand_off(db, actor, event_id, member_id, now=)` |
| Change meal time, change who does a role | `calls.set_meal_time(..., at: time)`, `calls.change_assignment(..., role, member_id or None)` |
| `/dinnerbell call` | `calls.start_call(db, actor, slot, day, now=)` |
| Admin: extend, shorten, or close a window | `call_admin.move_deadline(db, actor, event_id, Window.RESPOND / VOTE, minutes or None, now=)` |
| Admin: cancel, re-run, assign coordinator | `call_admin.cancel`, `call_admin.rerun`, `call_admin.assign_coordinator(..., member_id or None)` |

For what to show a member (their response, which buttons apply to them), use `services.event_views.event_view(db, actor, event_id, now=)`. Its `can` flags come from the same rules the services enforce.
