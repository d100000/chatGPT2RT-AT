# Third-party notices

## CPA2sub2API

The browser conversion modules and a subset of their tests are adapted from
[gtxx3600/CPA2sub2API](https://github.com/gtxx3600/CPA2sub2API),
commit [`42ff785d5f3527e4050c02a0da51d4f7eb4f1180`](https://github.com/gtxx3600/CPA2sub2API/tree/42ff785d5f3527e4050c02a0da51d4f7eb4f1180).

Adapted files:

- `rt_extractor/static/converter.mjs` from `docs/src/converter.mjs`
- `rt_extractor/static/archive.mjs` from `docs/src/archive.mjs`
- `rt_extractor/static/paste-input.mjs` from `docs/src/paste-input.mjs`
- Upstream cases in `tests/test_converter.mjs` and `tests/test_paste_input.mjs`

Local changes add compatibility with this project's raw Token JSON and Sub2API
v1 envelope, correct Unix timestamp handling, safely handle malformed dates and
UTF-8 JWT payloads, avoid echoing credential input in parse errors, and protect
archive paths and filenames from collisions. The UI uses the host project's
existing layout and interaction patterns.

Upstream license (retained in full):

```text
MIT License

Copyright (c) 2026 Hanhaofu

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
