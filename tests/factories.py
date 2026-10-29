def notice_doc(release_id: str, notice_type: str, doc_type: str = "tenderNotice") -> dict:
    return {"id": release_id, "documentType": doc_type, "noticeType": notice_type,
            "url": f"https://www.find-tender.service.gov.uk/Notice/{release_id}"}


def uk4(ocid="ocds-test-000001", rid="000100-2026", date="2026-10-01T09:00:00Z", tag="tender",
        deadline="2026-10-30T12:00:00Z", lots=("1",), cpv="39160000", region="UKE", docs=()) -> dict:
    return {
        "ocid": ocid, "id": rid, "date": date, "tag": [tag],
        "buyer": {"id": "GB-PPON-TEST", "name": "Northfield University"},
        "tender": {
            "title": "Student desks and seating", "status": "active",
            "legalBasis": {"id": "2023/54", "scheme": "UKPGA"},
            "tenderPeriod": {"endDate": deadline},
            "lots": [{"id": l, "status": "active"} for l in lots],
            "items": [{"id": l, "relatedLot": l,
                       "additionalClassifications": [{"scheme": "CPV", "id": cpv}],
                       "deliveryAddresses": [{"region": region}]} for l in lots],
            "documents": [*docs, notice_doc(rid, "UK4")],
        },
    }


def uk6(ocid="ocds-test-000001", rid="000300-2026", date="2026-11-20T09:00:00Z", lots=("1",)) -> dict:
    return {"ocid": ocid, "id": rid, "date": date, "tag": ["award", "contract"],
            "awards": [{"id": "1", "relatedLots": list(lots), "documents": [notice_doc(rid, "UK6", "awardNotice")]}]}


def uk12(ocid="ocds-test-000001", rid="000200-2026", date="2026-10-09T09:00:00Z") -> dict:
    return {"ocid": ocid, "id": rid, "date": date, "tag": ["tenderCancellation"],
            "tender": {"status": "complete", "documents": [notice_doc(rid, "UK12", "tenderCancellationNotice")]}}
