# Coordinating a meal

A coordinator steers one call: they keep the vote moving, and once the meal is decided they set the time, how the food arrives, and who cooks, orders, or picks it up. If nobody has claimed coordinating by the time voting starts, Dinnerbell picks someone who's in at random. Without a coordinator, the vote and the first person who offered decide everything. The precise rules are in [spec](spec.md) sections 5.7, 6.2, and 6.3 (FR-10, FR-58, FR-61).

## Become the coordinator

Tap **I'll coordinate** while answers are open or during the vote, when nobody else coordinates. The first person to tap it becomes the coordinator. You find the button in these places:

| Where | When |
|---|---|
| Your answer card (tap **Answer** on the call message) | While answers are open |
| App Home | While answers are open, and while voting |
| Your reminder message | While answers are open |
| The call message | While voting |
| The call's page on the web | While answers are open, and while voting |

**Coordinating puts you in.** If you said Out or Maybe, or hadn't answered, tapping **I'll coordinate** changes your answer to In. During the vote, you join late (FR-60 in the [spec](spec.md#63-proposals-ranking-and-voting)): you're marked late, any guests or eat window on an earlier answer are cleared, and, unlike other late joiners, your vote counts. If you joined late already, your vote counts from then on. A guest who coordinates is in, but never marked late, so their dietary needs still count.

The same rule applies when someone hands coordinating to you or an admin assigns it to you: you're in from then on, and if that changed your answer, you get a message saying so. After the decision, only someone who's already in can take over, because nobody joins a meal once it's decided. Someone the coordinator switched off during the vote can't take it on.

### If nobody claims it

When answers close and voting starts with nobody coordinating, Dinnerbell picks a coordinator at random from the members who said In. It never picks a guest or someone who was still a maybe. The call message names you, and you get a direct message saying you were picked and what you can do. You're a coordinator like any other: you can tap **Lock this**, break a tie, and shuffle the picks while voting, and once it's decided, change how you get it and who does it. When the decision leaves it unclear who does the role, you're asked to choose. If you don't within 15 minutes (an admin can change the time), or by the meal time if that's sooner, the first person who offered does it, if anyone did. You get credit for coordinating when the meal is done, as if you'd claimed it. If it doesn't suit you, hand it off on the call's page. Nobody gets picked when no member is in.

The pick is random, but reproducible: the same run of a call, with the same people in, always picks the same person. A re-run, or starting a called-off call again, picks again only if nobody coordinates by then.

A guest invited to the call can coordinate too, with fewer powers. See [What a guest coordinator can do](#what-a-guest-coordinator-can-do).

## What you can do at each stage

A call moves through three stages: answers, the vote, and the decision. The decision stays open after the meal until someone taps **Mark done**, or until two hours after the meal time, when Dinnerbell marks it done.

### While answers are open

| Action | Slack | Web |
|---|---|---|
| Close answers early (**Start the vote now**) | ⋯ menu on the call message, App Home | Call page |
| Call off the call | ⋯ menu on the call message, App Home | Call page |
| Hand off coordinating | No | Call page |

**Start the vote now** starts the vote at once, but the vote still ends at its usual time: the answer deadline plus the schedule's **Time to vote**. With fewer than two in, it ends the call instead, and everyone's on their own.

Answers also close on their own once everyone has answered:

- When two or more are in, answers close after nobody has changed an answer for 10 minutes, and once someone has offered to cook, pick up, or order. The vote starts then, and ends at the same usual time. Maybes are out from then on, and if one joins late, their vote counts.
- When everyone is out, the call ends at once, with no vote.
- When one person is in and everyone else is out, they get 10 minutes to add a guest. Otherwise the call ends.
- With fewer than two in, a maybe keeps answers open until the deadline, because they might still come.

### While voting

| Action | Slack | Web |
|---|---|---|
| Lock a proposal, which decides the call on it | **Lock this** on the proposals message | Call page |
| Make a takeout proposal pickup or delivery, when its place allows both | ⋯ beside it on the proposals message | Call page |
| Break a tie | **Break the tie** beside each tied proposal | Call page |
| Shuffle the proposals, up to 3 times | **Shuffle proposals** on the proposals message | Call page |
| Turn a late joiner's **Taking part** or **Vote counts** on or off | ⋯ beside them on the proposals message | Call page |
| Close the vote early (**Decide now**) | ⋯ menu on the call message, App Home | Call page |
| Call off the call | ⋯ menu on the call message, App Home | Call page |
| Hand off coordinating | No | Call page |

Anyone can add a proposal; you don't need to coordinate.

The vote runs until its usual time, even when everyone has voted, so people can look at every pick. To end it sooner, tap **Decide now**, **Lock this**, or **Break the tie**.

### Once it's decided, and after the meal

You can change any of these until the meal is marked done.

| Action | Slack | Web |
|---|---|---|
| Change the meal time | **Adjust** | **Change the time or who does what** |
| Switch how you get it: cook, pickup, or delivery, among the ways the place allows | **Adjust** (**How**) | **Assign roles** |
| Change who cooks, orders, or picks up, to anyone who's in | **Adjust** | **Assign roles**, or **Change the time or who does what** |
| Correct who did what after the meal, so the right person gets credit | **Adjust** | **Change the time or who does what** |
| Hand off coordinating to someone who's in | **Adjust** | Call page |
| Mark the meal done, once it's meal time | **Mark done** on the decision | Call page |
| Call off the meal | **Call it off** on the decision | Call page |

**Adjust** is on the decision message, your App Home card, and any message that asks you to pick someone for a role. It opens with the plan as it is, and saves only what you change. If someone changed the same thing elsewhere while you had it open, Adjust doesn't overwrite it; it asks you to open Adjust again.

Whoever you assign is told, and so is whoever no longer has the role. If they hadn't offered, the message says you assigned them, and that they can ask you to pick someone else. If you hand off in the same save, it names the new coordinator instead. From the meal time on, a change corrects the record and messages nobody.

Contributions and badges count who did what when the meal is marked done, so a correction you make before then is what counts. After that, only an admin can correct it, and the counts follow the correction.

### Takeout: who orders and who picks up

A home meal has a cook, and delivery has someone who orders. Pickup has two roles: someone orders it, and someone picks it up. By default they're different people: the first person who offered to pick up picks it up, and the first other person with **I'll order** on for the call orders. Someone who usually orders but turned **I'll order** off isn't picked, though they would be for delivery. If nobody else has it on, the person picking up orders too, so someone who offered to pick up is all a pickup needs. You can give one person every role, coordinating included.

While voting, you can make a takeout proposal pickup or delivery when its place allows both: ⋯ beside it on the proposals message, or the **Pickup** and **Delivery** buttons on the call page. Choosing the way it already has settles it too, and the proposal then says "(chosen)". An admin can do this as well.

When you switch how you get it, the person ordering keeps ordering. Switching from pickup to delivery drops whoever was picking up, and they get no pickup credit. Switching from delivery to pickup gives pickup to whoever you pick in **Who picks up**, or to the first person who offered to pick up.

### When the decision asks you to choose

The decision asks you right away, with a message that has **Assign roles**, when either of these is true:

- A role has no clear owner, because two people offered or nobody did. For pickup, ordering has no clear owner when more than one person besides the one picking up has **I'll order** on.
- The winning proposal is takeout at a place that allows pickup and delivery, and nobody chose its way: Dinnerbell drew it, and nobody set it during the vote. A proposal someone added, or an offer, has the way they chose.

In **Assign roles**, you choose how to get it, who cooks or orders, and who picks it up. If you change only one role, with **Change the time or who does what**, the other roles go to the first people who offered. You have 15 minutes to choose (an admin can change the time). After that, it stays the way it is, and the first people who offered do the roles.

## What an admin can do beyond that

| Action | Where | When |
|---|---|---|
| Assign, replace, or clear the coordinator | Web | Once the call is posted, until it's done; after the decision, only someone who's in |
| Extend or shorten the open window | Web | While answers are open, or while voting |
| Re-run a call | Web | Once voting has started, or after the call ended |
| Skip a day's call | Web | Before it goes out, today or up to 7 days ahead |
| Close a window early | Slack and web | While answers are open, or while voting |
| Call off the call | Slack and web | Until it's done, including before it posts |
| Lock a proposal | Slack and web | While voting |
| Make a takeout proposal pickup or delivery | Slack and web | While voting |
| Switch how you get it, and assign who does it | Web; in Slack, **Assign roles** while the role waits for a choice | Once decided |
| Change the meal time, or who cooks, picks up, or orders | Web | Once decided |
| Correct who did what after the meal is done | Web | Any time after |
| Break a tie, shuffle, switch late joiners | Slack and web | While voting, alongside the coordinator |

**Adjust** in Slack is the coordinator's alone. An admin who taps it is told to use the web.

When you correct who cooked, picked up, or ordered after the meal is done, Dinnerbell moves that meal's count from the old person to the new one. The new person earns any badge the count reaches, which is announced like any other. The old person loses each badge tier their counts no longer reach on the current counts, even one already announced, and even one first earned at a lower count before an admin raised it. A badge post still waiting to go out leaves it out, and a post about it that Slack already has stays. Correcting it back restores both. Each correction that moves a count writes an audit row, `event.contributions_recounted`, with who lost and gained the count and the badges taken back and awarded.

## What a guest coordinator can do

A guest coordinator handles the logistics, but not the choice of meal. They can close answers early, make a takeout proposal pickup or delivery during the vote, change the meal time, how you get it, and who does it, hand off, and mark the meal done. They can't vote, lock, break a tie, shuffle, switch late joiners, **Decide now**, or call off the call. While a guest coordinates, an admin can do those, and a tied vote goes to whoever invited the guest, then to an admin.

## When nobody coordinates, or someone drops out

| Situation | What happens |
|---|---|
| Nobody claims coordinating | When voting starts, Dinnerbell picks a member who's in at random, and tells them. If no member is in, nobody coordinates: the vote decides, a tie goes to the proposal more admins voted for, then the place eaten longest ago, then the order the proposals were drawn in, and the first person who offered does the role, which an admin can change. |
| The coordinator says Out | They stop coordinating. The call says nobody coordinates, and anyone can tap **I'll coordinate**, which puts them in. |
| The coordinator says Maybe | They keep coordinating. If they're still a maybe when answers close, they're out, and they stop coordinating. |
| The coordinator, as a late joiner, is switched off | They're out, so they stop coordinating. |
| An admin deactivates the coordinator | Nobody coordinates their open calls. |
| Nobody coordinates after the decision | Nobody can claim it once it's decided. An admin can assign someone who's in, on the web. |
| Someone assigned can't do it | They can't refuse in the app. They tell the coordinator, who picks someone else with **Adjust**. |
| An admin removes a guest who had a role | The role is cleared. The coordinator gets a message with **Adjust** to pick someone who's in; with nobody coordinating, each admin gets a message that links the web. |
| The call is called off | Everyone who was in or maybe, and the coordinator, is told. Anyone assigned to cook, pick up, or order gets a direct message. |
