# Test fixtures

`tests/test_onnx_provider.py` needs two real photographs here. They are deliberately **not**
committed: face photographs are personal data, and most of the convenient sample images are
not ours to redistribute.

| File | What it needs to be |
| --- | --- |
| `portrait.jpg` | one clear face, reasonably frontal |
| `group.jpg` | more than one face, none of them the person in `portrait.jpg` |

Any ordinary photographs will do — two colleagues who have agreed to it, or any face dataset
you already hold a licence for. Point the tests somewhere else with
`FA_TEST_FACE_DIR=/path/to/images` if you would rather not copy them into the repository.

Without these files the ONNX tests skip and the rest of the suite still runs.

`portrait2.jpg` is a second, *different* person — a single face, used to prove that two
different people score far apart. `tests/test_onnx_provider.py` compares it against
`portrait.jpg`. If you only have one portrait to hand, you can crop a single head out of
`group.jpg`, which is what it is.
