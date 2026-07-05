## Summary

Describe the change in a few sentences.

## Why This Change

What problem, operational need, or maintenance issue does this address?

## Target Branch

Target branch: <!-- main or website -->

- [ ] I confirmed that this PR targets the branch named above.

## Scope

- [ ] Runtime/service code
- [ ] Tests
- [ ] Examples or operator documentation
- [ ] Repository metadata, policy, or templates
- [ ] Website branch

## Validation

List the commands run and their results:

```text
python -m pytest
git diff --check
```

## Compatibility And Behavior

Describe user-visible behavior, configuration, protocol, deployment, or
downstream impact. Write `None` when there is no known impact.

- [ ] Legacy broadcast behavior was considered.
- [ ] Routing mode and target-scoped deduplication were considered where relevant.
- [ ] `source_id` and NMEA TAG `s` were not conflated.
- [ ] POSIX and Windows implications were considered where relevant.

## Documentation

- [ ] Documentation and examples were updated where needed.
- [ ] Planned functionality is not described as implemented.

## Security Checklist

- [ ] No unrelated changes are included.
- [ ] Tests/checks run above include results.
- [ ] `git diff --check` was run.
- [ ] No private keys, secrets, credentials, or sensitive operator data are included.
