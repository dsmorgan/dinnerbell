# Coordinating a meal

A coordinator steers one call: they keep the vote moving, and once the meal is decided they set the time, how the food arrives, and who cooks, orders, or picks it up. If nobody has claimed coordinating by the time voting starts, Dinnerbell picks someone who's in at random. Without a coordinator, the vote and the first person who offered decide everything. Only household members coordinate. The precise rules are in [spec](spec.md) sections 5.7, 6.2, and 6.3 (FR-10, FR-58, FR-61).

## Become the coordinator

Tap **I'll coordinate** while answers are open or during the vote, when nobody else coordinates. The first person to tap it becomes the coordinator. You find the button in these places:

| Where | When |
|---|---|
| Your answer card (tap **Answer** on the call message) | While answers are open |
| App Home | While answers are open, and while voting |
| Your reminder message | While answers are open |
| The call message | While voting |
| The call's page on the web | While answers are open, and while voting |

**Coordinating puts you in.** If you said Out or Maybe, or hadn't answered, tapping **I'll coordinate** changes your answer to In. During the vote, you join late (FR-60 in the [spec](spec.md#63-proposals-ranking-and-voting)): you're marked late, any guests or eat window on an earlier answer are cleared, and, unlike other late joiners, your vote counts. If you joined late already, your vote counts from then on.

The same rule applies when someone hands coordinating to you or an admin assigns it to you: you're in from then on, and if that changed your answer, you get a message saying so. After the decision, only someone who's already in can take over, because nobody joins a meal once it's decided. Someone the coordinator switched off during the vote can't take it on.

### If nobody claims it

When answers close and voting starts with nobody coordinating, Dinnerbell picks a coordinator at random from the members who said In. It never picks a guest or someone who was still a maybe. The call message names you, and you get a direct message saying you were picked and what you can do. You're a coordinator like any other: you can use **Lock this**, break a tie, and shuffle the picks while voting, and once it's decided, change how you get it and who does it. When the decision leaves it unclear who does the role, you're asked to choose. If you don't within 15 minutes (an admin can change the time), or by the meal time if that's sooner, the first person who offered does it, if anyone did. You get credit for coordinating when the meal is done, as if you'd claimed it. If it doesn't suit you, hand it off on the call's page. Nobody gets picked when no member is in.

The pick is random, but reproducible: the same run of a call, with the same people in, always picks the same person. A re-run, or starting a called-off call again, picks again only if nobody coordinates by then.

Guests don't coordinate. A guest can answer, offer to cook, pick up, or order, and be assigned those roles. See [What guests can't do](#what-guests-cant-do).

## Coordinate through the assistant

When the household's assistant is on, you can also ask it to coordinate a call for you, in a direct message with Dinnerbell or on the web's **Assistant** page, never in a channel thread: lock a pick, break a tie, shuffle the picks, change the plan as **Adjust** does, hand off coordinating, mark the meal done, or call the call off. Each request is a draft that says what **Confirm** does, and nothing changes until you confirm it. **Confirm** runs the same checks as the buttons in the tables below, so only the coordinator can, and an admin who isn't the coordinator is refused there too. See [The Assistant page](members.md#the-assistant-page).

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
| Lock a proposal, which decides the call on it | **Lock this** in the ⋯ beside it on the proposals message | Call page |
| Make a takeout proposal pickup or delivery, when its place allows both | ⋯ beside it on the proposals message | Call page |
| Break a tie | **Break the tie** in the ⋯ beside each tied proposal | Call page |
| Shuffle the proposals, up to 3 times | **Shuffle proposals** on the proposals message | Call page |
| Turn a late joiner's **Taking part** or **Vote counts** on or off | ⋯ beside them on the proposals message | Call page |
| Close the vote early (**Decide now**) | ⋯ menu on the call message, App Home | Call page |
| Call off the call | ⋯ menu on the call message, App Home | Call page |
| Hand off coordinating | No | Call page |

Anyone can add a proposal; you don't need to coordinate.

The vote runs until its usual time, even when everyone has voted, so people can look at every pick. To end it sooner, use **Decide now**, **Lock this**, or **Break the tie**. In Slack, **Lock this** and **Break the tie** ask you to confirm in a small window first. Everyone sees the ⋯ beside a proposal, and anyone who can't use these options is told so right away.

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

**Adjust** is on the decision message, your App Home card, and any message that asks you to pick someone for a role. It opens with the plan as it is, and saves only what you change. If someone changed the same thing elsewhere while you had it open, Adjust doesn't overwrite it; it asks you to open Adjust again.

Whoever you assign is told, and so is whoever no longer has the role. If they hadn't offered, the message says you assigned them, and that they can ask you to pick someone else. If you hand off in the same save, it names the new coordinator instead. From the meal time on, a change corrects the record and messages nobody, except that whoever you hand off to still gets a message, since the meal is theirs to finish.

### When you end up eating somewhere else

Change the place to record where you're really eating. Choose any place in the catalog or type a new name, even if nobody offered, someone vetoed it, or you just had it. Pick a way the place allows; a new name needs one, because **Cook** makes it a home meal and **Pickup** or **Delivery** a restaurant. People keep their roles if the new way still has them. A role nobody has yet goes to the first person who offered, or to nobody, for you to assign.

Each person who's in gets one message before the meal time, which says if they lost or gained a role, and the decision updates. From the meal time on, it corrects the record and messages nobody, except whoever you hand off to in the same save. Once the meal is marked done, only an admin can correct the place, on the web.

Contributions and badges count who did what when the meal is marked done, so a correction you make before then is what counts. After that, only an admin can correct it, and the counts follow the correction.

### Takeout: who orders and who picks up

A home meal has a cook, and delivery has someone who orders. Pickup has two roles: someone orders it, and someone picks it up. By default they're different people: the first person who offered to pick up picks it up, and the first other person with **I'll order** on for the call orders. Someone who usually orders but turned **I'll order** off isn't picked, though they would be for delivery. If nobody else has it on, the person picking up orders too, so someone who offered to pick up is all a pickup needs. You can give one person every role, coordinating included.

While voting, you can make a takeout proposal pickup or delivery when its place allows both: ⋯ beside it on the proposals message, or the **Pickup** and **Delivery** buttons on the call page. Choosing the way it already has settles it too, and the proposal then says "(chosen)". To put it back to not chosen, so you're asked when it wins, pick **[Either way]** in the ⋯, or tap the chosen way again on the call page. An admin can do this as well.

When you switch how you get it, the person ordering keeps ordering. Switching from pickup to delivery drops whoever was picking up, and they get no pickup credit. Switching from delivery to pickup gives pickup to whoever you pick in **Who picks up**, or to the first person who offered to pick up.

### When the decision asks you to choose

The decision asks you right away, with a message that has **Assign roles**, when either of these is true:

- A role has no clear owner, because two people offered or nobody did. For pickup, ordering has no clear owner when more than one person besides the one picking up has **I'll order** on.
- The winning proposal is takeout at a place that allows pickup and delivery, and nobody chose its way: Dinnerbell drew it, and nobody set it during the vote. A proposal someone added, or an offer, has the way they chose.

In **Assign roles**, you choose how to get it, who cooks or orders, and who picks it up. If you change only one role, with **Change the plan**, the other roles go to the first people who offered. You have 15 minutes to choose (an admin can change the time). After that, it stays the way it is, and the first people who offered do the roles.

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
| Change the place | Web | Once decided, until it's done |
| Correct who did what after the meal is done | Web | Any time after |
| Correct the place after the meal is done | Web, **Change the plan** | Any time after |
| Break a tie, shuffle, switch late joiners | Slack and web | While voting, alongside the coordinator |

**Adjust** in Slack is the coordinator's alone. An admin who taps it is told to use the web.

When you correct who cooked, picked up, or ordered after the meal is done, Dinnerbell moves that meal's count from the old person to the new one. The new person earns any badge the count reaches, which is announced like any other. The old person loses each badge tier their counts no longer reach on the current counts, even one already announced, and even one first earned at a lower count before an admin raised it. A badge post or monthly recap still waiting to go out leaves it out, and a post about it that Slack already has stays. Correcting it back restores both. Each correction that moves a count writes an audit row, `event.contributions_recounted`, with who lost and gained the count and the badges taken back and awarded.

When a meal was marked done before the household changed its mind, correct the place under **Change the plan** on the call's page. Choose any place in the catalog or type a new name, with a way the place allows, as you would before it's done. Only an admin can, and only on a call that's done.

- **History, stats, and ranking** use the corrected place, and the variety window counts it on the meal's date.
- **Feedback** on the meal moves to the corrected place. Answers that don't fit the new kind stop showing and counting: what went wrong on a home meal, and a thank-you on a restaurant. Correcting back brings them back, unless the person has saved their feedback since.
- **Roles** stay with the people who have them if the new way has them. A role the new way doesn't have is dropped, with its count. A new role goes to whoever offered first, or to nobody, and you can correct that right below it.
- **Counts and badges** for that meal follow the same rules as above, for cooking, picking up, ordering, suggesting, exploring, and adding the place to the catalog. Other meals keep theirs. Correcting the place back restores them for the roles it kept. A role the other way dropped doesn't come back on its own: it goes to whoever offered first, so if you had named someone else, name them again in the role form below.
- **Nobody gets a message.** The posted messages update, and a badge it earns is announced as usual.

A correction writes `event.place_changed`, marked `after_done`, and `event.contributions_recounted` if any count moved.

**Change the plan** is the only way to change a call's place once it's done. **Edit** in History shows that place read-only and links here, so the call, its messages, its counts, and its feedback all stay together. If a History edit from an earlier version left a meal and its call on different places, **Change the plan** says so and starts from the meal's place. Keep that place to bring the call into line, or choose the place the call still names to move the meal back. If the call's place has left the catalog, it isn't in the menu, so type its name and choose a way. Moving the meal back keeps the call's roles and how it won. Its counts stay too, unless other meals have changed how new the place was, as with any correction. If you deleted the meal from History, there's nothing to correct, and the form doesn't appear.

## What guests can't do

Coordinating means using Dinnerbell to steer a call, and guests have little or no access to it, so only household members coordinate. A guest is never offered coordinating, and can't be handed it or assigned it. Dinnerbell's random pick never picks one. A guest who taps **I'll coordinate** or **Mark done** on a channel message is told why not, and a guest can't use any other power on this page. An admin who makes a guest a member lets them coordinate from then on.

## When nobody coordinates, or someone drops out

| Situation | What happens |
|---|---|
| Nobody claims coordinating | When voting starts, Dinnerbell picks a member who's in at random, and tells them. If no member is in, nobody coordinates: the vote decides, a tie goes to the proposal more admins voted for, then the place eaten longest ago, then the order the proposals were drawn in, and the first person who offered does the role, which an admin can change. |
| The coordinator says Out | They stop coordinating. The call says nobody coordinates, and any member can tap **I'll coordinate**, which puts them in. |
| The coordinator says Maybe | They keep coordinating. If they're still a maybe when answers close, they're out, and they stop coordinating. |
| The coordinator, as a late joiner, is switched off | They're out, so they stop coordinating. |
| An admin deactivates the coordinator | Nobody coordinates their open calls. |
| Nobody coordinates after the decision | Nobody can claim it once it's decided. An admin can assign someone who's in, on the web. |
| Someone assigned can't do it | They can't refuse in the app. They tell the coordinator, who picks someone else with **Adjust**. |
| An admin removes a guest who had a role | The role is cleared. The coordinator gets a message with **Adjust** to pick someone who's in; with nobody coordinating, each admin gets a message that links the web. |
| The call is called off | Everyone who was in or maybe, and the coordinator, is told. Anyone assigned to cook, pick up, or order gets a direct message. |
