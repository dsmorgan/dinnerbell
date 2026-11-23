# Catalog format

The catalog is the household's list of things to eat: restaurants and home meals, each tagged by cuisine and anything else that matters for choosing. This page defines the catalog file that Dinnerbell imports and exports, which a person, a script, or an AI assistant can write.

## Ways to add to the catalog

Any member can add to the catalog:

- **In the app.** The **Catalog** page has a form to add one item at a time.
- **Bulk import.** On the **Catalog** page, select **Import a catalog file**, and paste or upload a file. Dinnerbell validates it, previews what it creates and changes, and applies it only after you confirm.
- **On first start.** See [Load a catalog on first start](configuration.md#load-a-catalog-on-first-start).
- **With an AI assistant.** Give it this page, the [schema](#validate-a-file), and your notes, such as "we get Thai from the place on Main, pickup or DoorDash", and ask for a catalog file. Import the result; the preview catches mistakes. [Load the catalog](setup.md#12-load-the-catalog) suggests other sources.
- **With Dinnerbell's assistant.** When the [assistant](configuration.md#the-assistant) is on, ask it to draft items from a menu or recipe link. **Confirm** opens the import preview.
- **From another instance.** See [Copy the catalog to another instance](#copy-the-catalog-to-another-instance).

## Example

This file declares tags, three restaurants, and two home meals:

```yaml
version: 1

tags:
  cuisine: [Thai, Mexican, Italian, Indian, Japanese, American, Mediterranean]
  protein: [chicken, beef, pork, seafood, vegetarian, vegan]
  dietary: [gluten-free option, dairy-free option, nut-free]
  other: [spicy, comfort, healthy, quick]

restaurants:
  - name: Example Thai Kitchen
    cuisine: [Thai]
    tags: [spicy, vegetarian, gluten-free option]
    modes: [pickup, delivery]
    url: https://example.com/thai-kitchen/menu
    order_url: https://www.doordash.com/store/example
    notes: Ask for medium spice. Pad see ew is the safe bet.

  - name: Example Trattoria
    cuisine: [Italian]
    tags: [comfort]
    modes: [pickup]
    meals: [dinner]

  - name: Example Bagels
    cuisine: [American]
    tags: [quick]
    meals: [breakfast, lunch]

home_meals:
  - name: Chicken tacos
    cuisine: [Mexican]
    tags: [chicken, quick, gluten-free option]
    servings: 6
    cooks: [cook@example.com]
    notes: Corn tortillas for the gluten-free option.

  - name: Spaghetti and meatballs
    cuisine: [Italian]
    tags: [beef, comfort]
    servings: 6
```

A complete starter file is in [seed/example.yaml](../seed/example.yaml).

## Fields

### Top level

| Field | Required | Meaning |
|---|---|---|
| `version` | Yes | Format version. Always `1` for this format. |
| `tags` | No | Tags to create, grouped by kind: `cuisine`, `protein`, `dietary`, `other`. |
| `restaurants` | No | List of restaurant entries. |
| `home_meals` | No | List of home meal entries. |

### Restaurant entry

| Field | Required | Meaning |
|---|---|---|
| `name` | Yes | Display name. Unique among restaurants, ignoring case. |
| `cuisine` | Yes | One or more cuisine tags. Variety tracking uses these. |
| `tags` | No | Any other tags: protein, dietary, or other. |
| `modes` | No | `pickup`, `delivery`, or both. Default: `[pickup]`. |
| `meals` | No | Which meals it's for: `breakfast`, `lunch`, `dinner`, or any mix. Default: `[lunch, dinner]`. See [Meals](#meals). |
| `url` | No | Menu or website link. |
| `order_url` | No | Link to order online, such as a delivery app page. |
| `notes` | No | Free text up to 500 characters. |
| `active` | No | `false` hides the item from proposals without deleting its history. Default: `true`. |

### Home meal entry

| Field | Required | Meaning |
|---|---|---|
| `name` | Yes | What's being made. Unique among home meals, ignoring case. |
| `cuisine` | Yes | One or more cuisine tags. |
| `tags` | No | Any other tags. |
| `meals` | No | Same as for restaurants. Default: `[lunch, dinner]`. |
| `servings` | No | Typical servings one batch makes, from 1 to 99. |
| `cooks` | No | Emails of active members who can make it. The preview reports and skips any other email. |
| `url` | No | Recipe link. |
| `notes` | No | Free text up to 500 characters. |
| `active` | No | Same as for restaurants. |

## Meals

An entry without `meals` is for lunch and dinner, because most places don't serve breakfast. List `breakfast` for a place that serves it, and drop `lunch` or `dinner` for a place that doesn't. A list needs at least one meal, so `meals: []` and `meals: null` are errors.

`meals` doesn't forbid anything. A call proposes items for its meal first, and fills the vote from other items, marked such as "Usually for dinner", only when too few fit. **Add a proposal** and **Make an offer** list the rest after, marked such as "(usually dinner)".

## Rules

- **Tags match by name, ignoring case.** `thai` and `Thai` are the same tag, and the first spelling Dinnerbell sees is kept.
- **Tag names are unique across kinds.** You can't have a cuisine and a protein both named `vegetarian`.
- **Lists hold the right kind of tag.** Every tag in a `cuisine` list must be a cuisine, and no tag in a `tags` list may be one. `vegetarian` under `cuisine`, or `Thai` under `tags`, is an error.
- **Undeclared tags are created,** as a `cuisine` if it appears in a `cuisine` list, and as `other` otherwise. The preview lists every tag it creates, so you can catch typos.
- **Names are unique within a file.** Two restaurants, or two home meals, whose names match ignoring case are an error.
- **Unknown fields are errors.** A misspelled field such as `mode:`, or a field of the other kind, such as `servings` on a restaurant, is reported, not ignored.
- **Imports never delete.** An entry whose name matches an existing item of the same kind updates it: fields in the file replace the stored values, and fields left out, including `meals` and `modes`, stay; defaults apply only to new items. Items missing from the file stay. To retire an item, set `active: false`.
- **An explicit empty value clears a field.** On an update, `notes: null`, `url: ""`, or `tags: []` removes the stored value.
- **Imports are all or nothing.** If any entry fails validation, nothing is saved, and the preview lists every error with its location, such as `restaurants[3].modes[0]: "drive-thru" is not one of pickup, delivery`. Positions count from 0, so `restaurants[3]` is the fourth restaurant.
- **Plain YAML only.** Dinnerbell refuses anchors and aliases (`&name`, `*name`), repeated keys in one mapping, nesting deeper than 20 levels, more than 50,000 values, and files over 1 MB.
- **Preferences aren't part of the file.** Each person sets their own preferences in the app.

## Export the catalog

An export is a catalog file of the whole catalog: every tag, and every restaurant and home meal, active or not. Use it to copy the catalog to another instance, to keep an editable copy, or to edit many items at once and import the result. Any member can select **Download catalog** on the **Catalog** page; on the host, `dinnerbell catalog export` writes the file to standard output ([Command-line options](#command-line-options)).

- **Every field is written,** with empty values as `null` or `[]`, so importing an export makes each matching item exactly what the export says.
- **Everything is sorted** by name, so two exports of the same catalog are identical, and a diff shows only real changes.
- **Cooks aren't exported,** because they're member emails. Importing an export leaves each item's cooks as they are, so set cooks on each instance.
- **Nothing personal is exported:** no members, emails, Slack IDs, preferences, vetoes, meal history, or audit records.
- **Import's limits apply:** at most 1 MB and 50,000 values, about 2,000 items. If an export passes either, the command warns you; split the file and import each part.

Importing an export into the catalog it came from previews as "The catalog already matches this file." Importing it into another adds missing items and tags, updates items that match by name, and deletes nothing.

### Copy the catalog to another instance

To copy a catalog between production and a [test instance](test-instance.md), export it from one and import it into the other. An export holds no personal data, but keep it out of public repositories: it's still your household's list of places and notes.

1. On the **Catalog** page, select **Download catalog**. Or, on the host, run the export in a one-off container. Keep `-T`: without it, compose runs the command in a terminal, which changes line endings and mixes messages into the file.

   ```sh
   podman-compose run --rm -T dinnerbell catalog export > catalog.yaml           # production
   deploy/dinnerbell-test run --rm -T dinnerbell catalog export > catalog.yaml   # test
   ```

   With secrets in files, add `-f compose.yaml -f compose.secrets.yaml` after `podman-compose`, or the export fails with `NAME is required but not set` ([Run compose with secret files](setup.md#run-compose-with-secret-files)).

2. In the other instance, on the **Catalog** page, select **Import a catalog file**, upload `catalog.yaml`, and select **Preview**.
3. Check what it adds and changes, then select **Apply**.

A tag with a different kind in each instance, such as `vegetarian` as a protein in one and as other in the other, is an error, and nothing is imported until you rename it. To retire an item in both instances, set it inactive in one and copy again.

### Command-line options

- `--output FILE` writes a file instead of standard output. It refuses to replace an existing file unless you add `--force`, which replaces only a regular file or a symlink (the symlink itself, not its target). It writes a new file and renames it into place, so the directory must let you create files.
- The command needs the app's configuration to find the database. It opens the database read-only and never migrates it, so it's safe while Dinnerbell runs.
- Run it inside the container, as the app's user, never as root on the host: SQLite can create `dinnerbell.db-wal` and `dinnerbell.db-shm` files owned by whoever ran it, which Dinnerbell then can't open.

<a id="validating-a-file"></a>

## Validate a file

The JSON Schema for this format is [seed/catalog.schema.json](../seed/catalog.schema.json). Editors with YAML language support validate against it when the file starts with this line, pointing at the schema:

```yaml
# yaml-language-server: $schema=./catalog.schema.json
```

From a checkout, validate a file without importing it:

```sh
uv run dinnerbell catalog validate path/to/catalog.yaml
```

The schema is generated from the format's models. After you change them, regenerate it with `uv run dinnerbell catalog schema > seed/catalog.schema.json`; a test fails while it's stale.
