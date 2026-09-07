# Report prompt for usage investigations

Use this prompt with caller-selected local evidence. It is a writing template;
the deterministic scripts make no model calls. Do not substitute example paths
with private paths in this public file.

> Analyze the supplied usage evidence for the stated interval and audience.
> Start with the most consequential unanswered question. Distinguish measured
> facts, heuristic classifications, assumptions, and untested explanations.
>
> State source coverage, exclusions, deduplication, timestamp semantics, source
> collection time, code revision, tariff provenance, and unknown costs. Call a
> model-operation observation what it is; do not rename it a user turn or HTTP
> request. Preserve input/cache/output category conservation. Reference tariff
> value is not an invoice, and missing sources do not establish a bill bound.
>
> Show costs by token category, harness/model, input length, task function,
> trigger origin, visible action, and timing where evidence supports them.
> Explain each denominator and label coverage. Mixed labels form combinations;
> overlapping candidate groups must not be added. A working directory does not
> establish a cost beneficiary. Inspect local original prompts for meaning,
> but do not copy excerpts or row-level examples into a publishable report.
>
> Separate the latest substantive task input, the latest wake input, and the
> immediate model input. A notification is not necessarily a human or peer
> instruction. Association with downstream work is not the notification's
> causal cost. Unknown origin, absent visible task, and unknown function remain
> unknown; none means idle or useless. Tool names show actions, not success.
>
> Compare wake-to-wake gaps with model-observation gaps. Exact provider request
> start times, prefix identity, cache settings, and routing require direct
> evidence. Explain assumptions behind timing bounds and check negative or
> missing times. Cache resets may reflect changed prefixes as well as elapsed
> time. Separate harnesses and models; report support, session counts, session
> bootstrap intervals, within-session comparisons, exceptions, and sensitivity
> to thresholds. A retrospective split is not independent validation.
>
> Seek short, interpretable rules with support and counterexamples. Describe
> temporal order before interpreting them: task complexity may cause both
> messages and cost; post-trigger context, actions, and output may be mediators
> or outcomes. Do not turn predictive feature importance into causal effects.
> Mark exploratory rules and specify future validation before testing it.
>
> Consider deleting unnecessary steps before adding mechanisms. For each
> proposed intervention, define task-level total cost and completion quality,
> count child agents, retries, summary/retrieval calls and rework, and explain
> randomization or another credible comparison. Removing a cache-writing call
> may move the same write to the next necessary call. Pattern-associated dollars
> are not savings. Externalized context helps only after including all reload
> and quality costs. Do not claim net savings without outcome evidence.
>
> Deliver a readable aggregate report, reproducible commands, evidence location,
> quality checks and remaining gaps. Keep session logs, row-level usage, prompt
> excerpts, per-session rankings, identifiers, source inventories, private
> configurations and scan logs on local disk outside Git. Commit only reviewed
> synthesis for the authorized audience. Keep general code and this prompt in
> the public package; never embed personal findings or operating context there.
