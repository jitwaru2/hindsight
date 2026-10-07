══════════════════════════════════════════════════════════════════════════
ATOMIC FACTS - ONE CLAIM PER FACT
══════════════════════════════════════════════════════════════════════════

Each fact is later filed under the thing it is about and the property it gives a value for,
and a newer fact on the same thing and property retires the older one. A fact holding two
claims cannot be filed, and retiring it loses the other claim. A decision never written as a
decision leaves the earlier "not yet accepted" fact standing as if nothing was decided.

The FOCUS section, when the request has one, decides what is worth retaining; these rules decide
how every fact is written, including the facts FOCUS asks for. Without a FOCUS section, skip
greetings, filler and process chatter.

DECISIONS
  - A deliberate action a person reports or takes (created, forked, bought, sent, cancelled,
    signed up for, hired, quit) is a decision carried out. Write it as two facts: the decision,
    "<person> chose to <do it>", and the state the action produced, with its details: "<the
    thing> is <what or where it now is>". Never write the action only as an event: an event is
    not filed as a decision and never settles the earlier proposal.
  - A matter settled is written as a resolution, naming who settled it, with the words that
    settle it: "<person> chose ...", "decided ...", "ruled that ...", "accepted ...", "rejected
    ...", "cancelled ...", "reversed ...".
  - A person settles a course of action by saying so, by treating it as decided (working out how
    it is to be done, or what it must allow or avoid), or by doing it. Write the resolution in
    every case, whether or not the proposal appears in the text and even when no one says "I
    choose". A question about a course of action, or a reply that only explores it, settles
    nothing.
  - A recommendation, proposal, offer or question from an assistant or another person belongs to
    them: write who made it and that it was not yet accepted, never as the decision.

THE RULES FOR EVERY FACT
1. One claim. "what" is one sentence with one main verb, stating one value of one property of
   one thing. Never join two actions, decisions, findings or values with "and", "also", "while",
   "then" or a semicolon: write two facts. A decision with its reason is one fact, the reason in
   "why"; each alternative turned down is its own fact. A list stays in one fact only when the
   whole list is the value of one property (the members of a team, the steps of one procedure).
   If a later statement could change one part of a fact and leave the rest true, the parts are
   separate facts. "who" lists only the people; "why" gives only the reason for this same claim.

2. The subject named. Open with what the fact is about, by its full name as the text gives it,
   never "it", "this", "the plan" or "the proposal". When a person decides or states something
   about a matter, name both and list both in "entities", the matter first.

3. The value in full. Amounts, dates, times, names, identifiers, file paths, URLs, versions and
   quoted words verbatim. Concise means one claim per fact, never a shortened value.

4. A state in the moment is provisional and says so. How a matter stood at a point, rather than
   how it was settled, is written with its moment and with words that mark it provisional: "As
   of <moment>, ... not yet accepted", "... not yet decided", "awaiting <person>'s word",
   "proposed", "recommended", "an open question", "tentative". The moment is the turn's timestamp
   when the content carries one, otherwise the date of the entry or of the document. A claim that
   stands is written without these words.

5. The final state of what changes. When something changes within the text, its latest state is
   written as standing. An earlier state is recorded only when the text stated it as the state
   at that point, and then as a provisional fact of its own: "As of <moment>, <earlier state>;
   this later changed." Never write an earlier state as standing or merge it into the final one.

6. Documents. Dated entries are statements as of their dates. Where a later entry changes a
   position, the latest entry's position stands and each earlier entry's position is an earlier
   state under rule 5, in every fact that states it, including the facts from the earlier entry's
   own section. Keep the document's own qualifications: an open question, a contradiction it
   notes, a status of superseded, withdrawn or amended.

EXAMPLES - for illustration only; never emit their names, facts or dates.

A conversation; the user is Dana Okafor.
  user, 2025-03-04T09:02Z: "Why are we patching the importer again instead of replacing it?"
  assistant, 2025-03-04T09:10Z: "I recommend replacing the importer with Airbyte. Nothing starts
  until you say so."
  user, 2025-03-05T10:15Z: "On the Airbyte move, we need to be able to roll back within a day."
  user, 2025-03-06T16:40Z: "I've set up the Airbyte workspace at acme.airbyte.io and moved the
  billing feed. I also forked the connector repo to github.com/dokafor/connectors."
Facts:
  - "As of 2025-03-04T09:10Z, the assistant's recommendation to replace the importer with Airbyte
    was not yet accepted by Dana Okafor."
  - "Dana Okafor chose to replace the importer with Airbyte."
  - "Dana Okafor required that the move to Airbyte can be rolled back within a day."
  - "Dana Okafor chose to set up an Airbyte workspace."
  - "The Airbyte workspace is at acme.airbyte.io."
  - "Dana Okafor chose to move the billing feed to Airbyte."
  - "The billing feed runs on Airbyte."
  - "Dana Okafor chose to fork the connector repository."
  - "Dana Okafor's fork of the connector repository is github.com/dokafor/connectors."
Wrong: "Dana Okafor set up the Airbyte workspace and moved the billing feed" (two claims joined by
"and", written as events, and the choices never written).
Wrong: "Dana Okafor forked the connector repository to github.com/dokafor/connectors." as the only
fact for the fork (an event; the choice to fork is never written).

A document dated 2025-06-10 about Dana Okafor, with two entries:
  "2025-06-09 - resumed sessions with Lee Park, every Monday at 8am."
  "2025-06-02 - paused sessions with Lee Park until the grant comes through."
Facts:
  - "Dana Okafor resumed sessions with Lee Park, every Monday at 8am, on 2025-06-09."
  - "As of 2025-06-02, Dana Okafor had paused sessions with Lee Park until the grant came
    through; this later changed."
