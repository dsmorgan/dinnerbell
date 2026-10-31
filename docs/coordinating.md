# Coordinating a meal

A coordinator steers one call: they keep the vote moving, and once the meal is decided they set the time, how the food arrives, and who cooks, picks up, or orders it. Coordinating is optional. Without a coordinator, the vote and the first person who offered decide everything. The precise rules are in [spec](spec.md) sections 5.7, 6.2, and 6.3 (FR-10, FR-58, FR-61).

## Become the coordinator

Tap **I'll coordinate** while answers are open or during the vote, when nobody else coordinates. The first person to tap it becomes the coordinator. You find the button in these places:

| Where | When |
|---|---|
| Your answer card (tap **Answer** on the call message) | While answers are open |
| App Home | While answers are open, and while voting |
| Your reminder message | While answers are open |
| The call message | While voting |
| The call's page on the web | While answers are open, and while voting |

**Coordinating puts you in.** If you said Out or Maybe, or hadn't answered, tapping **I'll coordinate** changes your answer to In. During the vote, you join late (FR-60 in the [spec](spec.md#63-proposals-ranking-and-voting)): you're marked late, any guests or eat window on an earlier answer are cleared, and, unlike other late joiners, your vote counts.

The same rule applies when someone hands coordinating to you or an admin assigns it to you: you're in from then on. After the decision, only someone who's already in can take over, because nobody joins a meal once it's decided.

A guest invited to the call can coordinate too, with fewer powers. See [What a guest coordinator can do](#what-a-guest-coordinator-can-do).

## What you can do at each stage

A call moves through three stages: answers, the vote, and the decision. The decision stays open after the meal until someone taps **Mark done**, or until two hours after the meal time, when Dinnerbell marks it done.

### While answers are open

| Action | Slack | Web |
|---|---|---|
| Close answers early (**Start the vote now**) | ⋯ menu on the call message, App Home | Call page |
| Call off the call | ⋯ menu on the call message, App Home | Call page |
| Hand off coordinating | No | Call page |

### While voting

| Action | Slack | Web |
|---|---|---|
| Lock a proposal, which decides the call on it | **Lock this** on the proposals message | Call page |
| Break a tie | **Break the tie** beside each tied proposal | Call page |
| Shuffle the proposals, up to 3 times | **Shuffle proposals** on the proposals message | Call page |
| Turn a late joiner's **Taking part** or **Vote counts** on or off | ⋯ beside them on the proposals message | Call page |
| Close the vote early (**Decide now**) | ⋯ menu on the call message, App Home | Call page |
| Call off the call | ⋯ menu on the call message, App Home | Call page |
| Hand off coordinating | No | Call page |

Anyone can add a proposal; you don't need to coordinate.

### Once it's decided, and after the meal

You can change any of these until the meal is marked done.

| Action | Slack | Web |
|---|---|---|
| Change the meal time | **Adjust** | **Change the time or who does what** |
| Switch how you get it: cook, pickup, or delivery, among the ways the place allows | **Adjust** (**How**) | **Assign roles** |
| Change who cooks, picks up, or orders, to anyone who's in | **Adjust** | **Assign roles**, or **Change the time or who does what** |
| Correct who did what after the meal, so the right person gets credit | **Adjust** | **Change the time or who does what** |
| Hand off coordinating to someone who's in | **Adjust** | Call page |
| Mark the meal done, once it's meal time | **Mark done** on the decision | Call page |
| Call off the meal | **Call it off** on the decision | Call page |

**Adjust** is on the decision message, your App Home card, and any message that asks you to pick someone for a role. It opens with the plan as it is, and saves only what you change. If someone changed the same thing elsewhere while you had it open, Adjust doesn't overwrite it; it asks you to open Adjust again.

When you switch how you get it, whoever you pick in **Who does it** does it the new way: for example, switching from pickup to delivery with Kim selected makes Kim the one who orders. When the decision leaves the role without a clear owner, because two people offered or nobody did, you get a message with **Assign roles** and 15 minutes to choose (an admin can change the time); after that, the first person who offered does it.

Whoever you assign is told. If they hadn't offered, the message says you assigned them, and that they can ask you to pick someone else. If you hand off in the same save, it names the new coordinator instead.

Contributions and badges count who did what when the meal is marked done, so a correction you make before then is what counts.

## What an admin can do beyond that

| Action | Where | When |
|---|---|---|
| Assign, replace, or clear the coordinator | Web | While the call is open; after the decision, only someone who's in |
| Extend or shorten the open window | Web | While answers are open, or while voting |
| Re-run a call | Web | Once voting has started, or after the call ended |
| Skip a day's call | Web | Before it goes out, today or up to 7 days ahead |
| Close a window early, or call off the call | Slack and web | While the call is open |
| Lock a proposal | Slack and web | While voting |
| Switch how you get it, and assign who does it | Web; in Slack, **Assign roles** while the role waits for a choice | Once decided |
| Change the meal time, or who cooks, picks up, or orders | Web | Once decided |
| Correct who did what after the meal is done | Web | Any time after |
| Break a tie, shuffle, switch late joiners | Slack and web | While voting, only when nobody coordinates or a guest does |

**Adjust** in Slack is the coordinator's alone. An admin who taps it is told to use the web. A correction after the meal is done doesn't change the contributions already counted.

## What a guest coordinator can do

A guest coordinator handles the logistics, but not the choice of meal. They can close answers early, change the meal time, how you get it, and who does it, hand off, and mark the meal done. They can't vote, lock, break a tie, shuffle, switch late joiners, **Decide now**, or call off the call. While a guest coordinates, an admin can do those, and a tied vote goes to whoever invited the guest, then to an admin.

## When nobody coordinates, or someone drops out

| Situation | What happens |
|---|---|
| Nobody claims coordinating | The vote decides. A tie goes to the proposal more admins voted for, then the place eaten longest ago, then the order the proposals were drawn in. The first person who offered does the role, and an admin can change it. |
| The coordinator says Out | They stop coordinating. The call says nobody coordinates, and anyone can tap **I'll coordinate**, which puts them in. |
| The coordinator says Maybe | They keep coordinating. If they're still a maybe when answers close, they're out, and they stop coordinating. |
| The coordinator, as a late joiner, is switched off | They're out, so they stop coordinating. |
| Nobody coordinates after the decision | Nobody can claim it once it's decided. An admin can assign someone who's in, on the web. |
| Someone assigned can't do it | They can't refuse in the app. They tell the coordinator, who picks someone else with **Adjust**. |
| An admin removes a guest who had a role | The role is cleared. The coordinator gets a message with **Adjust** to pick someone who's in; with nobody coordinating, each admin gets a message that links the web. |
| The call is called off | Everyone who was in or maybe, and the coordinator, is told. Anyone assigned to cook, pick up, or order gets a direct message. |
