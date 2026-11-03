import json

from beauty_leads.sources import csv_import, google_places, orgbook, osm, vancouver

GOOGLE_PLACE = {
    "id": "ChIJabc",
    "displayName": {"text": "Northpoint Barbers"},
    "formattedAddress": "128 Lonsdale Ave, North Vancouver, BC V7M 2E8, Canada",
    "addressComponents": [
        {"longText": "128", "shortText": "128", "types": ["street_number"]},
        {"longText": "Lonsdale Avenue", "shortText": "Lonsdale Ave", "types": ["route"]},
        {"longText": "North Vancouver", "shortText": "North Vancouver", "types": ["locality", "political"]},
        {"longText": "British Columbia", "shortText": "BC", "types": ["administrative_area_level_1", "political"]},
        {"longText": "V7M 2E8", "shortText": "V7M 2E8", "types": ["postal_code"]},
    ],
    "location": {"latitude": 49.31, "longitude": -123.08},
    "nationalPhoneNumber": "(604) 555-0104",
    "websiteUri": "https://www.vagaro.com/northpoint",
    "rating": 4.9, "userRatingCount": 455, "priceLevel": "PRICE_LEVEL_MODERATE",
    "businessStatus": "OPERATIONAL", "types": ["barber_shop", "point_of_interest"], "primaryType": "barber_shop",
    "googleMapsUri": "https://maps.google.com/?cid=1",
}


def test_google_parse_place():
    r = google_places.parse_place(GOOGLE_PLACE)
    assert r.name == "Northpoint Barbers" and r.city == "North Vancouver"
    assert r.street == "128 Lonsdale Avenue" and r.postal_code == "V7M 2E8"
    assert r.review_count == 455 and r.types[0] == "barber_shop"
    assert r.extra["google_maps_url"].startswith("https://maps.google.com")


def test_google_drops_other_provinces():
    other = json.loads(json.dumps(GOOGLE_PLACE))
    other["addressComponents"][3]["shortText"] = "AB"
    assert google_places.parse_place(other) is None


def test_google_collect_paginates_and_respects_budget(cfg, monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
    calls = []

    class Http:
        def post_json(self, url, body, headers=None, use_cache=True):
            from beauty_leads.http import HttpResult
            calls.append(body)
            page = {"places": [dict(GOOGLE_PLACE, id=f"p{len(calls)}")]}
            if "pageToken" not in body:
                page["nextPageToken"] = "tok"
            return HttpResult(url=url, status=200, text=json.dumps(page), headers={})

    recs = list(google_places.collect(cfg, Http(), cities=["Kelowna"], terms=["barber shop"]))
    assert [r.source_id for r in recs] == ["p1", "p2"]
    assert calls[0]["textQuery"] == "barber shop in Kelowna, BC"
    calls.clear()
    recs = list(google_places.collect(cfg, Http(), cities=["A", "B"], terms=["x"], max_requests=1))
    assert len(calls) == 1


def test_osm_parse_element():
    el = {"type": "way", "id": 7, "center": {"lat": 48.4, "lon": -123.3},
          "tags": {"name": "Coastline Cuts", "shop": "hairdresser", "hairdresser": "barber",
                   "addr:housenumber": "1420", "addr:street": "Douglas Street", "addr:city": "Victoria",
                   "contact:phone": "+1 250 555 0105", "brand": "Coastline"}}
    r = osm.parse_element(el)
    assert r.source_id == "way/7" and r.lat == 48.4
    assert "hairdresser=barber" in r.types
    assert r.phone == "+1 250 555 0105" and r.brand == "Coastline"
    assert osm.parse_element({"type": "node", "id": 1, "tags": {"shop": "beauty"}}) is None


def test_vancouver_facets_and_licence():
    facets = {"facets": [
        {"name": "businesstype", "facets": [{"name": "Personal Services", "value": "Personal Services", "count": 900},
                                            {"name": "Restaurant", "value": "Restaurant", "count": 4000}]},
        {"name": "businesssubtype", "facets": [{"name": "Barber Shop", "value": "Barber Shop", "count": 300}]},
    ]}
    m = vancouver.matching_facets(facets, ["barber", "personal service"])
    assert m == {"businesstype": ["Personal Services"], "businesssubtype": ["Barber Shop"]}
    row = {"businessname": "MAIN ST FADE ROOM LTD.", "businesstradename": "Main Street Fade Room",
           "licencenumber": "24-123456", "status": "Issued", "businesstype": "Personal Services",
           "businesssubtype": "Barber Shop", "unit": "2", "house": "3920", "street": "MAIN ST",
           "city": "Vancouver", "province": "BC", "postalcode": "V5V 3P2", "numberofemployees": "7.0",
           "folderyear": "24", "geo_point_2d": {"lat": 49.25, "lon": -123.1}}
    r = vancouver.parse_licence(row)
    assert r.name == "Main Street Fade Room" and r.num_employees == 7
    assert r.street == "2-3920 MAIN ST" and r.extra["legal_name"] == "MAIN ST FADE ROOM LTD."
    assert r.business_status == "OPERATIONAL"


def test_orgbook_parse_topic():
    topic = {"id": 1843754, "source_id": "FM0494666", "type": "registration.registries.ca",
             "names": [{"text": "800744401", "type": "business_number"},
                       {"text": "BELLORDS BARBER SHOP", "type": "entity_name"}], "addresses": [],
             "attributes": [{"type": "registration_date", "value": "2009-01-29T08:00:00+00:00"},
                            {"type": "entity_status", "value": "ACT"},
                            {"type": "entity_type", "value": "SP"}]}
    p = orgbook.parse_topic(topic)
    assert p["name"] == "BELLORDS BARBER SHOP" and p["registration_date"] == "2009-01-29"
    assert p["business_number"] == "800744401" and p["all_names"] == ["BELLORDS BARBER SHOP"]
    assert orgbook.excluded("BARBER MOTORS(1963)LTD.", ["motors"])
    assert not orgbook.excluded("BELLORDS BARBER SHOP", ["motors"])
    assert p["entity_type_label"] == "Sole proprietorship" and p["incorporated"] is False


def test_csv_import(cfg, tmp_path):
    f = tmp_path / "calls.csv"
    f.write_text("Business Name,Phone,City,Current Software,Monthly Spend,Chairs,Notes\n"
                 "Fadehouse Barber Co.,604-555-0101,Surrey,Vagaro,$200,6,hates add-on fees\n"
                 ",,,,,,\n")
    recs = list(csv_import.collect(cfg, file=str(f)))
    assert len(recs) == 1
    r = recs[0]
    assert r.source == "csv:calls" and r.num_employees == 6
    assert r.extra["known_software"] == "Vagaro" and r.extra["known_monthly_spend_cad"] == 200.0
    assert r.extra["Notes"] == "hates add-on fees"
