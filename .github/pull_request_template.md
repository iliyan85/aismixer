## Summary

Describe the problem and the approach taken.

## Changed Area

- [ ] AIS/NMEA parsing or multipart assembly
- [ ] TAG handling or source attribution
- [ ] Deduplication
- [ ] UDP input or forwarding
- [ ] Secure UDP or `nmea_sproxy`
- [ ] Configuration, installation, or service behavior
- [ ] Tests or documentation only

## Tests Run

List the commands and relevant results:

```text
python -m pytest
git diff --check
```

## Compatibility Notes

Describe any effect on existing configs, protocols, deployments, or downstream
consumers. Write `None` when there is no known compatibility impact.

## Checklist

- [ ] I kept the change focused and targeted the correct branch.
- [ ] I added or updated tests when practical.
- [ ] I ran the relevant tests and documented the results above.
- [ ] I documented compatibility or configuration impact.
- [ ] I removed private keys, credentials, and sensitive operational details.
- [ ] I updated operator or protocol documentation when behavior changed.
