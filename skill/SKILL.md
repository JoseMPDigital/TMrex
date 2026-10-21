---
name: tmrex
description: TMrex, trademark clearance search and registrability analysis for a proposed mark: local USPTO register, common-law web use and domains, written up as a report. Use to clear, screen or check a trademark or brand name.
---

# TMrex: trademark clearance

Produce a screening-level clearance report for a proposed mark: what's on the USPTO register, who is using similar names without registering them, which domains are taken, and how likely the mark is to be refused or to draw an objection. The reader is usually a lawyer or a founder; the report has to show its work, so every conclusion points to a record, a URL or a stated assumption.

The tools live in the `tmrex` folder (see its README): `~/tmrex` when running directly on the Mac, or `$HOME/mnt/tmrex` when the folder is connected to a desktop-app session. Find it first with `ls -d ~/tmrex $HOME/mnt/tmrex 2>/dev/null`, and use that path wherever this skill says `~/tmrex`. Run every script with `python3 -I`. If `domain_check.py` can't reach the network, check the domains with web search instead and say so.

| Tool | What it does |
| --- | --- |
| `search/clearance_search.py clearance --mark "X" --classes 009,042` | Battery of register searches (exact, all words, each word, variants, compounds, pseudo-marks, near spellings), de-duplicated and ranked |
| `search/clearance_search.py query '<json>'` | One custom register search: Boolean queries on mark, goods, owner and statements, plus class, status and date filters (syntax in `search/search_register.py`, `TOOL_DEFINITION`) |
| `search/clearance_search.py record SERIAL ...` | Full records |
| `search/domain_check.py X --names a,b` | DNS and RDAP status across common TLDs |

If `~/tmrex/data/uspto.db` doesn't exist, this machine doesn't have the register: say so, and run only the web and domain parts, labelled as such.

## Getting started

If you're invoked without a mark (for example just `/tmrex`, or "what can TMrex do?"), don't start searching. Reply with this, filled in with the register date from `data/sync_status.json`:

> **TMrex** runs a screening-level trademark clearance: the USPTO register (current through <date>), the web for unregistered use, and domain names, then a report with a risk rating.
>
> Tell me:
> 1. **The mark**, spelled exactly as you'll use it.
> 2. **What it's for**, in plain words ("project management software", "coffee shop").
> 3. *Optional:* classes you want covered, where you'll use it, and whether it's already in use and since when.
>
> For example: *"Clear BLUE HERON for project management software, in use since 2024."* A full report takes a few minutes and is saved to `reports/`.

## 1. Intake

You need the mark exactly as it will be used, and the goods or services in plain words. Ask only for what's missing. Also useful if offered: where it will be used, whether it's already in use and since when, and the stylization or logo (this tool clears the words only).

Then pick the classes:
- The Nice classes the goods fall in. Prefer USPTO ID Manual wording when you describe them.
- Related classes where an examiner would find the goods related. Common pairings: software 009 with SaaS 042; clothing 025 with retail 035; beer 032 with wine and spirits 033 and restaurants and bars 043; food 029/030 with restaurants 043; cosmetics 003 with retail 035; entertainment 041 with recorded media 009; financial services 036 with apps 009 and 042; medical devices 010 with pharmaceuticals 005.
State the classes you chose and why in the report.

## 2. Register search

1. Check how current the register is: `cat ~/tmrex/data/sync_status.json`. Cite `daily_files_through` in the report. The sync runs on its own twice a day, so the date is normally a day or two behind. If `last_run` is more than 2 days old, the scheduled sync has stopped: say so, and offer to run `python3 -I ~/tmrex/sync/sync_daily.py` now (it takes up to about 15 minutes and needs network access, so it won't work from a desktop-app sandbox).
2. Run the battery with the target and related classes. Add `--include-dead` once at the end, to note dead marks as possible common-law users.
3. Follow up with `query` for what the battery can't do:
   - **Foreign equivalents.** Translate each distinctive word into the languages an American buyer would likely know (Spanish, French, German, Italian, Chinese and others as relevant) and search the translations.
   - **Same meaning, different words.** Synonyms and marks with the same commercial impression (HERON and EGRET; RED BULL and RAGING BULL).
   - **A word inside a compound** (GOWAVE for WAVE). Full-text search can't match the middle of a word, so search the likely compounds by name (`"gowave" OR "mywave" OR "thewave"`) and note the gap.
   - **Crowded field.** Count live marks in the class that share each word: `{"mark_query": "wave", "classes": ["009"], "status": ["registered", "pending"], "limit": 0}`. Many coexisting registrations mean a weak word and a narrower scope of protection.
   - **Descriptiveness evidence.** Search disclaimers of each word for these goods: `{"statements_query": "wave", "statement_types": ["disclaimer"], "classes": ["009"], "limit": 20}`. Frequent disclaimers suggest the word is descriptive.
   - **Famous marks.** If any word is a well-known brand, check it across all classes (dilution doesn't need related goods).
4. Pull full records with `record` for the 10 to 15 closest candidates before analysing them. Read the actual identification of goods, the status and dates, and the owner.

## 3. Common-law use (web search)

Search the web for: the exact mark with the goods or services; the mark alone; the main spelling variants; the mark on Amazon, the app stores, LinkedIn and Crunchbase as relevant; and social handles. For each real user in a related field, record the URL, the business, the goods, the apparent geographic reach, and the earliest use you can see. Note that state business-entity registries and unindexed local businesses aren't covered.

## 4. Domains

Run `domain_check.py` on the mark and two or three natural variants (with a hyphen, with "get" or "try", the plural). For any taken .com, note whether it's in active use and by whom, since that also counts as common-law evidence.

## 5. Analysis

Keep registrability (will the USPTO refuse it?) separate from use risk (will someone object to it?). They often differ.

**Distinctiveness.** Place the mark on the spectrum: generic, descriptive, suggestive, arbitrary, fanciful. Explain why for these goods. Flag Section 2 issues:
- 2(e)(1) merely descriptive or deceptively misdescriptive (immediately conveys a quality, feature, function or characteristic of the goods);
- 2(e)(2)/(3) geographic;
- 2(e)(4) primarily merely a surname;
- 2(a) deceptive, or a false connection with a person or institution;
- 2(b) flags and insignia;
- 2(c) the name of a living person without consent;
- failure to function (common phrases, informational slogans).
Use disclaimer evidence from the register where you found it.

**Likelihood of confusion (Section 2(d) and infringement).** For each serious candidate, work through the DuPont factors that matter:
1. Similarity of the marks in appearance, sound, connotation and commercial impression. Identify the dominant portion; the first word is often dominant, and disclaimed words carry less weight.
2. Relatedness of the goods or services as identified in the registration, not as actually sold.
3. Trade channels and buyers. An unrestricted identification is presumed to move in all normal channels to all usual buyers.
4. Conditions of sale and purchaser sophistication.
5–6. Strength of the prior mark, conceptual and commercial, including the crowded-field count.
Plus any other factor the facts raise. Remember that an earlier-filed pending application can block too, and that dead records aren't 2(d) bars but can show common-law users.

**Overall rating.**
- *High:* a live mark that is identical or nearly so, for the same or closely related goods; or a famous mark.
- *Moderate:* a similar (not near-identical) live mark for related goods, an identical mark for goods that are only arguably related, a real descriptiveness problem, or a common-law user in the same field.
- *Low:* nothing live is similar for related goods, the mark is at least suggestive, and no relevant common-law use turned up.
A crowded field can lower the risk from any one prior mark. Say what would change the rating.

## 6. The report

Save it as Markdown to `~/tmrex/reports/YYYY-MM-DD-<mark>.md` and give a short summary in chat. Sections, in order:

1. **Summary.** The mark, the goods, the overall rating, the top three conflicts in one line each, and a recommendation: proceed, proceed with changes (say which), or choose another mark.
2. **Goods and classes.** The goods, the classes searched, and why the related classes were included.
3. **Distinctiveness and registrability.** Where the mark sits on the spectrum, and any Section 2 refusal risks.
4. **Register conflicts.** A table of the closest live marks: mark, serial or registration number, owner, status, classes, the relevant goods, and the main DuPont points. Below it, a short analysis of each serious conflict. Dead marks of note go in a separate short list.
5. **Common-law use.** Findings with links and dates.
6. **Domains.** A table of status by TLD.
7. **Search scope and limits.** The register date; every search run (copy `searches_run`, plus your follow-up queries and web searches); and what wasn't covered: state registrations, foreign registers, design marks and logos, unindexed use, and anything the battery's limits note.
8. **Disclaimer.** Use this text: "This report is an automated screening search, not legal advice and not a full clearance opinion. It doesn't create an attorney-client relationship. Search results can be incomplete, and trademark rights can exist without registration. Have a qualified trademark attorney review the results before you adopt, file or invest in this mark."

## Rules

- Cite only records that came back from the tools, with their serial or registration numbers, and URLs you actually opened. Never fill a gap from memory; if you think a conflicting mark exists but didn't find it, say so as an open question.
- Quote goods and services from the records, don't paraphrase them into something broader.
- Prefer the few conflicts that matter over a long list. Explain the top ones well.
- State every assumption (classes chosen, the meaning of the mark, the likely buyers).
- Don't infer who the requester is, or which marks they own, from account details such as their name or email address. If a record looks like it may belong to the requester (same name as the mark, or a matching firm), ask before treating it as theirs. Until they confirm, analyse it as a third-party conflict.
