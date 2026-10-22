# Catalog format

The catalog is the household's list of things to eat: restaurants and home meals, each tagged by cuisine and anything else that matters for choosing. This page defines the file format Dinnerbell imports. The same format is the target for future assistant-drafted entries, so anything that can produce it, whether a person, a script, or a chatbot, can feed the catalog.

## Ways to add to the catalog

- **In the app.** The Catalog page has an add form for one item at a time. Any member can use it.
- **Bulk import.** Paste or upload a catalog file on the Catalog page. Dinnerbell validates it, shows a preview of what will be created and changed, and applies it only after you confirm. Any member can import.
- **First run.** If `DINNERBELL_SEED_FILE` points to a catalog file, Dinnerbell imports it on first start, while the catalog is still empty. At that point only the admin is a member, so other cooks are skipped. After inviting everyone, import the same file again from the Catalog page to fill in the cooks, before editing those items in the app.
- **With a chatbot, today.** Give any chatbot this page plus your notes ("we get Thai from Siam Garden on Main, pickup or DoorDash") and ask for a catalog file. Import the result; the preview catches mistakes.
- **With the built-in assistant, later.** Milestone M6 adds an assistant that drafts entries in this format from a description or a menu link. Its drafts go through the same preview before anything is saved.

## Example

```yaml
version: 1

tags:
  cuisine: [Thai, Mexican, Italian, Indian, Japanese, American, Mediterranean]
  protein: [chicken, beef, pork, seafood, vegetarian, vegan]
  dietary: [gluten-free option, dairy-free option, nut-free]
  other: [spicy, comfort, healthy, quick]

restaurants:
  - name: Siam Garden
    cuisine: [Thai]
    tags: [spicy, vegetarian, gluten-free option]
    modes: [pickup, delivery]
    url: https://example.com/siam-garden/menu
    order_url: https://www.doordash.com/store/example
    notes: Ask for medium spice. Pad see ew is the safe bet.

  - name: Luigi's
    cuisine: [Italian]
    tags: [comfort]
    modes: [pickup]

home_meals:
  - name: Chicken tacos
    cuisine: [Mexican]
    tags: [chicken, quick, gluten-free option]
    servings: 6
    cooks: [david@example.com]
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
| `servings` | No | Typical number of servings one batch makes. Used to warn before over-inviting. |
| `cooks` | No | Email addresses of members who can make it. Emails that don't match a member are reported in the preview and skipped. |
| `url` | No | Recipe link. |
| `notes` | No | Free text up to 500 characters. |
| `active` | No | Same as for restaurants. |

## Rules

- **Tags match by name, ignoring case.** `thai` and `Thai` are the same tag. The first spelling Dinnerbell sees is kept.
- **Tag names are unique across kinds.** You can't have a cuisine and a protein both named `vegetarian`.
- **Lists hold the right kind of tag.** Every tag in a `cuisine` list must be a cuisine tag, and no tag in a `tags` list may be one. Listing `vegetarian` under `cuisine`, or `Thai` under `tags`, is an error.
- **Undeclared tags are created.** A tag used in an entry but missing from `tags` is created with kind `cuisine` if it appears in a `cuisine` list, otherwise `other`. The preview lists every tag it will create so you can catch typos.
- **Names are unique within a file.** Two restaurants, or two home meals, whose names match ignoring case are an error.
- **Unknown fields are errors.** A misspelled field such as `mode:`, or a field that belongs to the other kind, such as `servings` on a restaurant, is reported rather than ignored.
- **Imports never delete.** An entry whose name matches an existing item of the same kind updates it: fields present in the file replace the stored values, and fields left out stay as they are. Items missing from the file are left alone. To retire an item, set `active: false`.
- **An explicit empty value clears a field.** On an update, `notes: null`, `url: ""`, or `tags: []` removes the stored value, while leaving the field out keeps it.
- **Imports are all or nothing.** If any entry fails validation, nothing is saved and the preview lists every error with its location, such as `restaurants[3].modes[0]: "drive-thru" is not one of pickup, delivery`. List positions count from 0, so `restaurants[3]` is the fourth restaurant.
- **Plain YAML only.** Anchors and aliases (`&name`, `*name`), repeated keys in one mapping, nesting deeper than 20 levels, and more than 50,000 values are refused, as are files over 1 MB.
- **Preferences aren't part of the file.** Each person sets their own vetoes and likes in the app, and other members never see them.

## Validating a file

The JSON Schema for this format is in [seed/catalog.schema.json](../seed/catalog.schema.json). Editors with YAML language support validate against it when the file starts with:

```yaml
# yaml-language-server: $schema=./catalog.schema.json
```

From a checkout, validate without importing:

```
uv run dinnerbell catalog validate path/to/catalog.yaml
```

The schema is generated from the format's models. After changing them, regenerate it with `uv run dinnerbell catalog schema > seed/catalog.schema.json`; a test fails while it's stale.
