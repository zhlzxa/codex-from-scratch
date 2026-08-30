# What you are allowed to do right now

Filesystem sandboxing decides which files you can read or change.
`sandbox_mode` is `{mode}`: {mode_meaning}

Approvals decide what happens to everything the sandbox does not already
allow. `approval_policy` is `{policy}`: {policy_meaning}

A refusal from this layer starts with `Permission denied:`, not with `Error:`.
It is not a failure of your command and it will not change if you send the same
command again. {what_to_do}
