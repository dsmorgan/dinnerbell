# Coordinating a meal

A coordinator steers one call: they keep the vote moving, and once the meal is decided they set the time, how the food arrives, and who cooks, orders, or picks it up. Without a coordinator, the vote and the first person who offered decide everything. Only household members coordinate. The precise rules are in [spec](spec.md) sections 5.7, 6.2, and 6.3.

## Become the coordinator

When nobody coordinates, tap **I'll coordinate** while answers are open or during the vote. The first person to tap it becomes the coordinator. You find the button in these places:

| Where | When |
|---|---|
| Your answer card (tap **Answer** on the call message) | While answers are open |
| App Home | While answers are open, and while voting |
| Your reminder message | While answers are open |
| The call message | While voting |
| The call's page on the web | While answers are open, and while voting |

**Coordinating puts you in.** If you said Out or Maybe, or hadn't answered, coordinating changes your answer to In. The same applies when someone hands it to you or an admin assigns it to you, and you get a message if that changes your answer. During the vote, you [join late](spec.md#63-proposals-ranking-and-voting): any guests or eat window on an earlier answer are cleared, and, unlike other late joiners, your vote counts. After the decision, only someone who's already in can take over. A late joiner who's been switched off can't take it on.

### If nobody claims it

When voting starts with nobody coordinating, Dinnerbell picks a member who said In at random, never a guest or a maybe, and nobody when no member is in. The call message names you, and you get a direct message saying what you can do. You're a coordinator like any other, with credit for coordinating when the meal is done. If it doesn't suit you, hand it off on the call's page. A re-run, or starting a called-off call again, picks again if nobody coordinates by then.

## Coordinate through the assistant

When the household's assistant is on, you can ask it to coordinate for you in a direct message with Dinnerbell or on the web's **Assistant** page, never in a channel thread. Nothing changes until you select **Confirm**, which runs the same checks as the buttons below. An admin who isn't the coordinator can't coordinate there. See [The Assistant page](members.md#the-assistant-page).

## What you can do at each stage

A call moves through three stages: answers, the vote, and the decision. The decision stays open until someone taps **Mark done**, or until two hours after the meal time, when Dinnerbell marks it done.

### While answers are open

| Action | Slack | Web |
|---|---|---|
| Close answers early (**Start the vote now**) | ⋯ menu on the call message, App Home | Call page |
| Call off the call | ⋯ menu on the call message, App Home | Call page |
| Hand off coordinating | No | Call page |

Whoever started an ad-hoc call can also call it off, until it's decided.

**Start the vote now** starts the vote at once, but the vote still ends at its usual time: the answer deadline plus the schedule's **Time to vote**. With fewer than two in, it ends the call instead, and everyone's on their own.

Answers also close on their own once every member has answered:

- When two or more are in, and someone can cook, pick up, or order, answers close after 10 minutes with no changed answer. Maybes are out from then on, and if one joins late, their vote counts.
- When everyone is out, the call ends at once.
- When one person is in and everyone else is out, they get 10 minutes to add a guest. Otherwise the call ends.
- With fewer than two in, a maybe keeps answers open until the deadline.

### While voting

| Action | Slack | Web |
|---|---|---|
| Lock a proposal, which decides the call on it | **Lock this** in the ⋯ beside it on the proposals message | Call page |
| Make a takeout proposal pickup or delivery, when its place allows both | ⋯ beside it on the proposals message | Call page |
| Break a tie | **Break the tie** in the ⋯ beside each tied proposal | Call page |
| Shuffle the proposals, up to 3 times | **Shuffle proposals** on the proposals message | Call page |
| Turn a late joiner's **Taking part** or **Vote counts** on or off | ⋯ beside them on the proposals message | Call page |
| Move the decision time 15 minutes earlier, or 15 or 30 minutes later | **Move the decision time** in the ⋯ menu on the call message | Call page |
| Close the vote early (**Decide now**) | ⋯ menu on the call message, App Home | Call page |
| Call off the call | ⋯ menu on the call message, App Home | Call page |
| Hand off coordinating | No | Call page |

Any member who's in can add a proposal, which signs them up for its role.

The vote runs until its decision time, even when everyone has voted. To end it sooner, use **Decide now**, **Lock this**, or **Break the tie**, or move the decision time into the past. In Slack, **Lock this** and **Break the tie** ask you to confirm first.

### Once it's decided, and after the meal

You can change any of these until the meal is marked done.

| Action | Slack | Web |
|---|---|---|
| Change the meal time | **Adjust** | **Change the plan** |
| Change the place: any catalog place, or a new name | **Adjust** | **Change the plan** |
| Switch how you get it: cook, pickup, or delivery, among the ways the place allows | **Adjust** (**How**) | **Assign roles** |
| Change who cooks, orders, or picks up, to anyone who's in | **Adjust** | **Assign roles**, or **Change the plan** |
| Correct who did what after the meal, so the right person gets credit | **Adjust** | **Change the plan** |
| Hand off coordinating to a member who's in | **Adjust** | Call page |
| Mark the meal done, once it's meal time | **Mark done** on the decision | Call page |
| Call off the meal | **Call it off** on the decision | Call page |

**Adjust** is on the decision message, your App Home card, and any message that asks you to pick someone for a role. It saves only what you change. If someone changed the same thing elsewhere while you had it open, it asks you to open Adjust again instead of overwriting.

Before the meal time, whoever you assign is told, and so is whoever loses the role; if they hadn't offered, the message says you assigned them and that they can ask you to pick someone else. From the meal time on, a change corrects the record and messages nobody, except whoever you hand off to. Contributions and badges count who did what when the meal is marked done, so a correction you make before then is what counts.

### When you end up eating somewhere else

Change the place to record where you're really eating: any place in the catalog, or a new name, even one nobody offered or someone vetoed. Pick a way the place allows; a new name needs one, because **Cook** makes it a home meal and **Pickup** or **Delivery** a restaurant. People keep their roles if the new way still has them. A role nobody has yet goes to the first person who offered, or to nobody, for you to assign. Before the meal time, everyone else who's in gets one message about the new place, saying if they lost or gained a role. Once the meal is marked done, only an admin can correct the place, on the web.

### Takeout: who orders and who picks up

A home meal has a cook, and delivery has someone who orders. Pickup has two roles. By default, the first person who offered to pick up picks it up, and the first other person with **I'll order** on for the call orders. If nobody else has it on, the person picking up orders too, so someone who offered to pick up is all a pickup needs. You can give one person every role, coordinating included.

While voting, you or an admin can make a takeout proposal pickup or delivery when its place allows both, and the proposal then says "(chosen)". To unset it, so you're asked when it wins, pick **[Either way]** in the ⋯, or tap the chosen way again on the call page.

When you switch how you get it, the person ordering keeps ordering. Switching from pickup to delivery drops whoever was picking up, and they get no pickup credit. Switching from delivery to pickup gives pickup to whoever you pick in **Who picks up**, or to the first person who offered to pick up.

### When the decision asks you to choose

The decision asks you right away, with a message that has **Assign roles**, when either of these is true:

- A role has no clear owner, because two people offered or nobody did. For pickup, ordering has no clear owner when more than one person besides the one picking up has **I'll order** on.
- The winning proposal is takeout at a place that allows pickup and delivery, and nobody chose its way during the vote.

In **Assign roles**, you choose how to get it and who does each role. You have 15 minutes (an admin can change this), or until the meal time if that's sooner. After that, the first people who offered do the roles. If you change only one role, with **Change the plan**, the other roles go to the first people who offered.

## What an admin can do beyond that

| Action | Where | When |
|---|---|---|
| Assign, replace, or clear the coordinator | Web | Once the call is posted, until it's done; after the decision, only someone who's in |
| Extend or shorten the open window | Web | While answers are open, or while voting |
| Re-run a call | Web | Today's or a later call, while voting or decided, or after it ended or was called off |
| Skip a day's call | Web | Before it goes out, today or up to 7 days ahead |
| Close a window early, lock, break a tie, shuffle, switch late joiners, move the decision time, or choose pickup or delivery | Slack and web | Whenever the coordinator could, alongside them |
| Call off the call | Slack and web | Until it's done, including before it posts |
| Change the meal time, the place, how you get it, or who does each role | Web; in Slack, how you get it and who does it, with **Assign roles** while a role waits for a choice | Once decided (the place until it's done) |
| Mark the meal done | Slack and web | Once decided, even before the meal time |
| Correct who did what, or the place, after the meal is done | Web | Any time after |

**Adjust** in Slack is the coordinator's alone. An admin who taps it is told to use the web.

A re-run of a voting or decided call starts it over with a new call message. The old messages say **Started again** and lose their buttons, and each person who was to cook, pick up, or order, other than you, is told their role is off.

Correcting who did what after the meal is done moves that meal's counts, and can award or take back badges, even ones already announced. [Contributions and badges](contributions.md#when-an-admin-corrects-a-done-meal) has the rules.

When the meal is already done and the household ate somewhere else, correct the place under **Change the plan** on the call's page, as you would before it's done. Nobody gets a message; the posted messages update.

- **History, stats, and ranking** use the corrected place, and the variety window counts it on the meal's date.
- **Feedback** on the meal moves to the corrected place. Answers that don't fit the new kind stop showing and counting: what went wrong on a home meal, and a thank-you on a restaurant. Correcting back brings them back, unless the person has saved their feedback since.
- **Roles** stay with the people who have them if the new way has them. A role the new way doesn't have is dropped, with its count. A new role goes to whoever offered first, or to nobody, and you can correct that right below it. Correcting back gives a dropped role to whoever offered first, so if you had named someone else, name them again.

**Change the plan** is the only way to change a call's place once it's done; **Edit** in History shows it read-only and links here. If a History edit from an earlier version left a meal and its call on different places, **Change the plan** says so and starts from the meal's place: keep it to bring the call into line, or choose the call's place to move the meal back. If the call's place has left the catalog, type its name and choose a way.

## What guests can't do

A guest is never offered coordinating, handed it, assigned it, or picked at random, and can't use any other power on this page. A guest who taps **I'll coordinate** or **Mark done** on a channel message is told why not. Once an admin makes a guest a member, they can coordinate.

## When nobody coordinates, or someone drops out

| Situation | What happens |
|---|---|
| Nobody claims coordinating | When voting starts, Dinnerbell picks a member who's in at random. If no member is in, the vote decides, with [the usual tie rules](ranking.md#why-the-proposals-differ-between-calls), and the first person who offered does each role, which an admin can change. |
| The coordinator says Out | They stop coordinating, and any member can tap **I'll coordinate**, which puts them in. |
| The coordinator says Maybe | They keep coordinating. If they're still a maybe when answers close, they're out, and they stop coordinating. |
| The coordinator, as a late joiner, is switched off | They're out, so they stop coordinating. |
| An admin deactivates the coordinator | Nobody coordinates their open calls. |
| An admin deactivates a member who's in | Before the meal time, they're out of the call: their answer is out, their votes while voting are taken back, and any role they had is cleared, with a message to the coordinator, or each other admin, as for a removed guest. After the meal time, the call keeps what they did. Offers stay as they are, and reactivating them puts nothing back. |
| Nobody coordinates after the decision | Nobody can claim it. An admin can assign someone who's in, on the web. |
| Someone assigned can't do it | They can't refuse in the app. They tell the coordinator, who picks someone else with **Adjust**. |
| An admin removes a guest who had a role | Before the meal time, the role is cleared. The coordinator gets a message with **Adjust** to pick someone who's in; with nobody coordinating, each other admin gets a message that links the web. After the meal time, the call keeps what they did. |
| The call is called off | Everyone who was in or maybe, and the coordinator, is told. Anyone assigned to cook, pick up, or order gets a direct message. |
