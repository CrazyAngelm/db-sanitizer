# DB Sanitizer performance smoke

- Run ID: `perf-1000000`
- Provider: `fake` / `test-fake`
- Rows: 1500100 (requested at least 1000000)
- Distinct mappings: 400
- Accepted LLM items: 400 (equals distinct mappings)
- Dump+transform rows/sec: 3979.84
- Peak RSS bytes: 91779072

| Collect | LLM | Dump+transform | Restore | Verify |
| ---: | ---: | ---: | ---: | ---: |
| 1.187 | 6.559 | 376.925 | 2.724 | 88.251 |
