# UK Find a Tender amendment pilot

This purposive collection covers 24 procurements and 48 original or correction notices. It includes construction, health and social care, education catering, professional services, printing, transport, IT, pharmaceuticals and other sectors. The public notice data come from UK Find a Tender under the Open Government Licence. Each record contains the government archive URL, original XML filename, SHA-256 digest, notice URL and a frozen projection of relevant XML fields. [Publisher's data documentation](https://www.find-tender.service.gov.uk/Developer/Documentation) · [Open Government Licence](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/)

## Contents and sampling

- **sources.json** contains actual notice fields and F14 change text. It omits contact details from the XML header. The full official files can be recovered by filename from the recorded daily archives.
- **deadline_labels.json** contains 20 linked original/correction pairs. Inclusion required that the original submission date match the F14 old value and the replacement date differ. Both extensions and shortened deadlines are included. Each label identifies its evidence and the changed field. A hypothetical supplier is ready on a date between the two deadlines or at midnight on the later date. Outcomes concern submission timing only.
- **insurance_label.json** records the Birmingham SQ question, evidence locators, thresholds, alternative commitment route, hypothetical policy and scoped verdicts. The F14 supplies the earlier wording. The original tender notice does not contain the SQ attachment. Using that wording in a v1 simulation is a retrospective reconstruction.
- **scope_labels.json** contains three further cases for review. Catering retains a certification requirement while adding a school. The military memorials procurement links a submission instruction to DEFFORM 47. Network Rail changes the estimate for one lot, which provides a negative control for extraction of supplier requirements.

The labels were prepared and checked against the public source fields during this review. Human procurement adjudication, a second annotator and a held-out partition are pending. The collection contains selected requirements and controls. It is unsuitable as gold for every obligation in a tender pack. The three scope controls are annotation-only in this release. A missing field is unknown.

## Reproduce

From the repository root, verify downloaded XML locally or fetch the recorded archives.

```sh
python3 scripts/verify_versionrag_sources.py --xml-dir /path/to/extracted-xml
python3 scripts/verify_versionrag_sources.py
```

Run the paired replay and the separate synthetic controls with the development dependencies installed.

```sh
bash scripts/test.sh -s tests/test_versionrag.py tests/test_versionrag_eval.py
VERSIONRAG_ENCODERS=production bash scripts/test.sh -s tests/test_versionrag.py tests/test_versionrag_eval.py
```

The evaluation adapter projects the selected XML fields into local release fixtures. It synthesises one comparison lot for each deadline probe. The questionnaire probe uses the published old and new text, a fixed parser for one question and a fictional insured supplier. Neither adapter reads expected labels to produce the changed amount or replacement deadline. Review results and exclusions in [the evaluation](../../../docs/VERSIONRAG_EVALUATION.md).
