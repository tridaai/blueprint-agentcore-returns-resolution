# The red team (R19)

Round 19 is adversarial, aimed at what Rounds 15–18 built —
the quarantine (R15), access control (R16), management
reporting (R17) and carrier exceptions (R18) — and at the
seams where those rounds compose with the money, fraud and
messaging machinery underneath them. The method is R14's:
every attack was derived from the packs and the stated laws
first, then run; a failure is a defect (fixed in this round,
the attack kept as a regression) or a derivation error (the
pack wins, corrected here). Probes were run against the
unmodified R18 code before any fix; the verdicts below are
what the probes showed.

Nine defects were found and fixed. The rest held. The
regressions live in `tests/test_r19.py`, numbered to match
(test_rt01 … test_rt13), and the eval gate carries a
zero-tolerance counter per defect family.

## Defects found and fixed

**RT-01 — A claim could settle twice: the money layer counted
acts the claim's own state fold never honoured. (HIGH)**
Claim acts fold by `(acted_on, act_id)` — caller-supplied
dates and a content hash. Acknowledge and settle a claim on
the same day and the settle act's hash can sort *before* the
acknowledgement's: the fold then reads the claim as merely
acknowledged, forever, while a settled act of record stands.
The write door, judging state from the same fold, accepts a
**second** settlement — and `money_totals` summed the settled
amount over *every* settled act, fold or no fold. The probe's
money summary read **$251.95 settled against a $151.95
claim**. No adversary is needed beyond an unlucky hash; a
back-dated settle act (dated before the acknowledgement)
produces the same desync deterministically. **Fix, four
parts:** the fold orders same-day acts by the lifecycle the
module states (acknowledgement before settlement/denial);
`record_claim_act` refuses an act dated before the claim's
filing or before the standing acts (acts land in date order);
`money_totals`, the money summary and its verifier count
only the **effective** settlement — the one act the fold
honours per claim; `claim_view` reports that same act's
amount. Regression: T-RT1.

**RT-02 — A claim settled after it was denied. (HIGH)**
The same root: record the acknowledgement on day 10, record
a denial *dated* day 9, and the fold never lets the denial
take effect — the claim reads acknowledged, and a settlement
is accepted over a standing denial act. **Fix:** RT-01's date
guards and lifecycle-ordered fold; a denial recorded in
order is terminal, and an out-of-order one is refused at the
door. Regression: T-RT2.

**RT-03 — A top-up executed under an open carrier
exception. (HIGH)** `execute_refund` honours the R18 carrier
guard; `execute_top_up` never called it. On a damaged-in-
transit case graded *better* than declared (no transit
attribution, so a top-up falls due), with the approval gate
satisfied for the graded computation, the top-up paid out
while the damaged exception stood open and `carrier_block`
was set — money moving on an unaccounted journey, against
the exception pack's stated consequence ("refund execution
stays refused while the exception stands unresolved").
**Fix:** `execute_top_up` calls the same carrier guard as
`execute_refund`, immediately after the fraud guard. The
probe's same-label route is in fact closed twice over —
RT-04's grading_cleared resolves that damaged exception at
grading time — so the regression keeps the surviving route:
a top-up standing open from the first label's grading while
an exception stands on a replacement label issued after it.
Regression: T-RT3.

**RT-04 — A damaged exception whose item graded unharmed
could never resolve: the case sat in limbo. (MEDIUM)** A
damaged exception's only substantive resolution is grading's
transit attribution, which requires the graded condition to
be *worse* than declared. Grade the item exactly as declared
(or better) and the report attributes nothing, no act the
pack offers resolves the exception (escalation is
non-terminal), and the carrier guard bars the refund
permanently — against R18's own law that a case never sits
in limbo. **Fix:** a second derived resolution,
`grading_cleared`, on the `grading_attribution` precedent:
a finalised grading report on the exception's own label
that did not attribute the damage to transit resolves the
exception — the damage was examined and the item was not
worsened. No carrier claim opens on it (the claim gate
still requires the attributed report); the exception notice
gains a pack wording for the outcome. Regression: T-RT4.

**RT-05 — The exception pack's blame vocabulary was not in
the messaging verifier's ban list. (MEDIUM)** R18 stated
that no customer payload about an exception may carry the
pack's blame terms, and its eval gate scans the payloads
its own fixtures produce — template text only. The verifier
that gates every drafted message never loaded the terms:
as model notes on the exception notice, "your fault", "you
damaged", "damaged it yourself" and "damaged by you"
verified **clean and were delivered**. (Two terms were
refused only incidentally, by the condition-fact patterns.)
**Fix:** the verified universe derives its forbidden terms
from the exception pack as it already does from the scoring
pack — the ban cannot drift from the pack. Regression:
T-RT5.

**RT-06 — Claim filing on the API distinguished real from
invented exception ids for a customer. (LOW)**
`POST /cases/{id}/claims` looked the exception up (404)
before judging the caller's act (403), so a customer
principal — who may not file at all — learned whether an
exception id exists. **Fix:** with a principal carried, the
act is judged before the lookup, so an unentitled caller
gets the one act refusal whatever id they name. The Runtime
surface already judged the act first. Regression: T-RT6.

**RT-07 — The Runtime's R18 actions silently dropped a
payload actor that was not the principal. (LOW)** The
fraud-review, approval, gateway and quarantine branches —
and the API's R18 endpoints — refuse a body naming someone
else, on the record (`actor_mismatch`). The Runtime's
exception-act, claim-file and claim-act branches ignored
the claimed name and recorded the act under the principal:
no impersonation, but also no refusal and no record, where
the layer's own rule is "refused, never silently rewritten".
**Fix:** the three branches deny a mismatched claimed
actor/filer exactly as their siblings do. Regression: T-RT7.

**RT-08 — A whitespace-padded principal role authenticated.
(LOW)** The access layer stripped the carried pair before
judging it, so `" staff_supervisor "` — and `"system "` —
authenticated as the role, against the exact-vocabulary
discipline the gateway pack is graded on (T-P1). **Fix:**
the pair is judged as carried: padding is a malformed
principal, and a role is the pack's string or an unknown
role. Regression: T-RT8.

**RT-09 — The claim verifier accepted a claim bound to a
superseded computation. (LOW)** Verification checked only
that the bound computation was *among* the case's
computations; the claim of record assembles over the
**current** computation, and its verifier now says so.
At filing a claim always binds the head, so the check costs
no honest claim anything at filing; it bites when the
record later moves — a grading recorded after a lost
filing, over a voided-and-replaced label journey — which is
exactly the drift a verifier exists to name. Regression:
T-RT9.

## Attacks that held

**RT-10 — The exception paths do not bypass the fraud
hold.** A fraud-held case cannot even take a label (the
label tool calls the refund-path guard), so no journey —
and no exception — begins; and `execute_refund` on a held
case is fraud-barred whatever its exceptions say.

**RT-11 — `refund_as_received` does not bypass the approval
tiers.** The resolution lifts the carrier guard and nothing
else: a dual-tier refund resolved this way still refuses
with no approval, with one approval, and with the same
approver twice (refused at the recording door); two
different approvers execute it.

**RT-12 — Quarantined text does not launder through the
exception flows.** The T-Q1 hostile note, planted at intake
on a case then walked through the full lost-in-transit
journey: the request of record carries the note stripped,
the item stands held, and neither the exception notice, the
return summary, nor any management report carries the probe
string or its figure. Offered as a *model* note, the same
text is refused by the figure check ($999.99 equals no
computed value). No component reads message text as
instructions — messages are terminal artefacts — so the
instruction itself is inert prose wherever it is stopped.

**RT-13 — A top-up and a carrier claim cannot double-pay
the same case.** The two recoveries are structurally
exclusive: a damaged claim requires transit attribution,
which suppresses the grading adjustment a top-up settles;
a lost claim's case is never graded at all. Graded both
ways in the suite, with the money summary's lines kept
separate throughout.

**RT-14 — Customer probes on the new verify endpoints are
uniform.** `GET /exceptions/{id}/verify` and
`GET /claims/{id}/verify` judge the surface before any
existence check, so a customer gets the one surface refusal
for real and invented ids alike. (The filing endpoint was
the exception — RT-06.)

**RT-15 — Resolved exceptions take no acts; detection does
not duplicate.** Re-graded over the R18 journeys: a second
detection pass returns the standing artefacts, and every
act on a resolved exception is refused naming the resolving
act.

**RT-16 — A "found" parcel cannot re-enter after a
lost-refund resolution.** The warehouse receipt requires
the carrier's `received` event; the stalled journey's
record never carries one, so receipt and grading stay
refused after the refund-as-received execution — the
house's recovery is the claim, as the pack states.

**RT-17 — Execution before detection is the stated
boundary, not a bypass.** The carrier guard judges
exceptions *of record*; detection is the operation that
raises them. A refund executed before any detection pass
is judged against an empty exception record and proceeds
through every other gate; the moment detection runs, the
guard binds. Operators run detection on the carrier feed's
cadence — the same shape as the price watch in the sibling
blueprint — and the record says which ran when.

## The R14 named gaps, re-examined

**The 94.99 construction gap — still closed by
construction.** Rounds 15–18 added no scoring signal and
changed no contribution; every contribution in the scoring
pack is still a whole number of points, so every reachable
total is integral and 94.99 remains a value the band
function grades but the engine cannot produce. The R19
gate re-grades the integrality invariant over the fixture
record.

**The split-household link (T-F3) — still open, and still
a data decision.** Nothing built since R14 links two
customer accounts: memory retrieves by exact customer id,
access scopes by the principal's own id, and no pack names
a household key. Re-graded unchanged: CUST-HHA and
CUST-HHB score 52.00 MONITOR on their own histories,
disjoint inputs; merged they would score 76.00
FRAUD_REVIEW. Closing this needs an artefact of record
that links the accounts — a data decision for the
retailer, not a scoring tweak.

**Opener/reviewer separation in fraud review — still
open, because the pack still does not state it.** The
scoring pack governs the review's outcome and reason
vocabulary and says nothing about who reviews; there is
still no opener in the machinery to separate a reviewer
from. Where a pack *does* state separation — grading,
where the receiver of a parcel may not grade it — the
separation is enforced and re-graded here (T-RT10 in the
suite numbering of `tests/test_r19.py`). Inventing a
review rule the pack does not state would grade fiction.

## Boundaries the round relied on

- **Carrier events are the port's record.** The local
  double's re-scripting is a harness act, reachable through
  no service surface; the service guarantees are over the
  artefacts the events raise.
- **A carried principal is trusted as terminated
  identity** (R16's stated scope): any caller can *claim*
  the `system` role at these local surfaces, exactly as
  they can call without a principal. RT-08 closes a
  normalization wart inside that scope; the scope itself
  is the deployment's Identity layer to change, and the
  README states it.
- **A fully self-consistent forgery is indistinguishable
  from a filing** in a content-addressed design with no
  signatures: the verifiers catch claims that *disagree*
  with the record (RT-09 tightens one such disagreement).
  The store is inside the trust boundary; its raw reads
  stay behind the quarantine's guarded accessors.
