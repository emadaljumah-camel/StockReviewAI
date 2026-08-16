import base64
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

from location_context import (
    ReverseGeocodeResult,
    clear_reverse_geocode_cache,
    extract_gps_from_jpeg_base64,
    gps_from_ifd,
    resolve_location_context,
    reverse_geocode_gps,
    strip_jpeg_metadata_base64,
)
from lmstudio_client import _build_payload, finalize_assessment
from main import app
from models import (
    LMStudioSettings,
    GeocodingSettings,
    LocationContext,
    LocationContextLevel,
    ModelAssessment,
    ReviewRequest,
    ReviewResponse,
)
from prompt import (
    RESPONSE_SCHEMA,
    SYSTEM_PROMPT,
    determine_location_context_level,
    location_context_prompt,
    location_log_fields,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_MAIN = PROJECT_ROOT / "StockReviewAI.lrplugin" / "StockReviewAI.lua"
PLUGIN_CLIENT = PROJECT_ROOT / "StockReviewAI.lrplugin" / "AiClient.lua"
JPEG_BASE64 = "/9j/"
NORMALIZED_SYSTEM_PROMPT = " ".join(SYSTEM_PROMPT.split())


def jpeg_base64_with_gps(
    latitude=(2.0, 16.0, 0.0),
    latitude_ref="S",
    longitude=(40.0, 54.0, 0.0),
    longitude_ref="E",
):
    image = Image.new("RGB", (2, 2), "white")
    exif = Image.Exif()
    exif[0x8825] = {
        1: latitude_ref,
        2: latitude,
        3: longitude_ref,
        4: longitude,
    }
    output = BytesIO()
    image.save(output, "JPEG", exif=exif)
    return base64.b64encode(output.getvalue()).decode("ascii")


def request_data(**overrides):
    data = {
        "filename": "DSC_9251.jpg",
        "image_base64": JPEG_BASE64,
        "threshold": 70,
    }
    data.update(overrides)
    return data


def assessment() -> ModelAssessment:
    return ModelAssessment.model_validate(
        {
            "technical_score": 75,
            "composition_score": 75,
            "commercial_value_score": 75,
            "editorial_value_score": 75,
            "main_subject_type": "discrete",
            "main_subject_visibility": "clear",
            "foreground_dominates_frame": False,
            "composition_defect_severity": "none",
            "composition_defects": [],
            "human_activity_readability": "none",
            "editorial_evidence": ["unusual_documentary_condition"],
            "recognizable_people": False,
            "release_sensitive_property": False,
            "prominent_ip_content": False,
            "hard_reject": False,
            "hard_reject_reasons": [],
            "reason": "Visible facts support the assessment.",
            "issues": [],
        }
    )


class LocationRequestValidationTests(unittest.TestCase):
    def test_old_request_without_location_remains_valid(self):
        request = ReviewRequest.model_validate(request_data())
        self.assertIsNone(request.location_context)

    def test_complete_and_partial_contexts_are_valid(self):
        complete = ReviewRequest.model_validate(
            request_data(
                location_context={
                    "country": "Kuwait",
                    "state_or_province": "Al Asimah",
                    "city": "Kuwait City",
                    "sublocation": "Al-Mubarakiya",
                }
            )
        )
        partial = ReviewRequest.model_validate(
            request_data(location_context={"city": "Kuwait City"})
        )
        self.assertEqual(complete.location_context.country, "Kuwait")
        self.assertEqual(partial.location_context.city, "Kuwait City")

    def test_whitespace_is_trimmed(self):
        request = ReviewRequest.model_validate(
            request_data(location_context={"city": "  Kuwait City  "})
        )
        self.assertEqual(request.location_context.city, "Kuwait City")

    def test_empty_or_whitespace_only_context_is_rejected(self):
        for context in ({}, {"city": "   "}):
            with self.subTest(context=context):
                with self.assertRaises(ValidationError):
                    ReviewRequest.model_validate(
                        request_data(location_context=context)
                    )

    def test_non_string_and_unknown_fields_are_rejected(self):
        for context in ({"city": 123}, {"district": "Sharq"}):
            with self.subTest(context=context):
                with self.assertRaises(ValidationError):
                    ReviewRequest.model_validate(
                        request_data(location_context=context)
                    )

    def test_endpoint_accepts_existing_lightroom_json_format(self):
        expected = ReviewResponse(
            verdict="YES",
            score=75,
            reason="Visible facts support the assessment.",
            issues=[],
        )
        with patch(
            "main.review_with_lmstudio",
            new=AsyncMock(return_value=expected),
        ):
            response = TestClient(app).post("/review", json=request_data())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected.model_dump())


class LocationLevelTests(unittest.TestCase):
    def test_each_location_level(self):
        cases = (
            (None, LocationContextLevel.NONE),
            (LocationContext(country="Kuwait"), LocationContextLevel.COUNTRY),
            (
                LocationContext(state_or_province="Al Asimah"),
                LocationContextLevel.REGION,
            ),
            (LocationContext(city="Kuwait City"), LocationContextLevel.CITY),
            (
                LocationContext(sublocation="Al-Mubarakiya"),
                LocationContextLevel.SUBLOCATION,
            ),
        )
        for context, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    determine_location_context_level(context), expected
                )

    def test_most_specific_supplied_field_wins(self):
        context = LocationContext(
            country="Kuwait",
            state_or_province="Al Asimah",
            city="Kuwait City",
            sublocation="Al-Mubarakiya",
        )
        self.assertEqual(
            determine_location_context_level(context),
            LocationContextLevel.SUBLOCATION,
        )


class LocationPromptTests(unittest.TestCase):
    def test_prompt_contains_only_supplied_location_values(self):
        prompt = location_context_prompt(
            LocationContext(country="Kuwait", city="Kuwait City")
        )
        self.assertIn("Country: Kuwait", prompt)
        self.assertIn("City: Kuwait City", prompt)
        self.assertNotIn("State or province:", prompt)
        self.assertNotIn("Sublocation:", prompt)

    def test_prompt_warns_when_context_is_absent(self):
        prompt = location_context_prompt(None)
        self.assertIn(
            "No verified textual location context was supplied.", prompt
        )
        self.assertIn("unsupported inference", prompt)

    def test_lm_studio_payload_includes_verified_context(self):
        request = ReviewRequest.model_validate(
            request_data(location_context={"city": "Kuwait City"})
        )
        payload = _build_payload(request, LMStudioSettings())
        user_text = payload["messages"][1]["content"][0]["text"]
        self.assertIn("VERIFIED LOCATION CONTEXT", user_text)
        self.assertIn("City: Kuwait City", user_text)

    def test_gps_source_is_described_without_coordinates(self):
        prompt = location_context_prompt(
            LocationContext(country="Kenya", city="Lamu"),
            "gps_reverse_geocode",
        )
        self.assertIn("embedded GPS reverse-geocoded", prompt)
        self.assertIn("Country: Kenya", prompt)
        self.assertNotIn("-2.2", prompt)
        self.assertNotIn("40.9", prompt)

    def test_system_prompt_prohibits_guessing_and_numeric_bonus(self):
        self.assertIn(
            "must not create a numeric bonus", NORMALIZED_SYSTEM_PROMPT
        )
        self.assertIn(
            "Never infer or name a more specific location",
            NORMALIZED_SYSTEM_PROMPT,
        )

    def test_location_does_not_enter_model_or_public_response_schema(self):
        assessment_fields = set(
            ModelAssessment.model_json_schema()["properties"]
        )
        response_fields = set(ReviewResponse.model_json_schema()["properties"])
        self.assertNotIn("location_context", assessment_fields)
        self.assertNotIn("identifiable_location", assessment_fields)
        self.assertEqual(
            response_fields, {"verdict", "score", "reason", "issues"}
        )
        self.assertEqual(
            RESPONSE_SCHEMA, ModelAssessment.model_json_schema()
        )


class LocationPolicyTests(unittest.TestCase):
    def test_location_has_no_deterministic_score_bonus(self):
        value = assessment()
        without_location = ReviewRequest.model_validate(request_data())
        with_location = ReviewRequest.model_validate(
            request_data(location_context={"city": "Kuwait City"})
        )
        self.assertEqual(
            finalize_assessment(value, without_location.threshold),
            finalize_assessment(
                value,
                with_location.threshold,
                with_location.location_context,
            ),
        )

    def test_log_fields_are_server_derived(self):
        absent = location_log_fields(None)
        present = location_log_fields(
            LocationContext(country="Kuwait", city="Kuwait City")
        )
        self.assertFalse(absent["verified_location_context"])
        self.assertEqual(absent["location_context_level"], "none")
        self.assertTrue(present["verified_location_context"])
        self.assertEqual(present["location_context_level"], "city")
        self.assertEqual(present["location_country"], "Kuwait")
        self.assertEqual(present["location_city"], "Kuwait City")
        self.assertEqual(present["location_sublocation"], "")

    def test_log_fields_do_not_contain_coordinates(self):
        fields = location_log_fields(
            LocationContext(country="Kenya", city="Lamu"),
            source="gps_reverse_geocode",
            gps_metadata_present=True,
            reverse_geocoder_status="ok",
        )
        self.assertEqual(fields["location_context_source"], "gps_reverse_geocode")
        self.assertTrue(fields["gps_metadata_present"])
        self.assertNotIn("latitude", fields)
        self.assertNotIn("longitude", fields)


class GpsExtractionTests(unittest.TestCase):
    def test_embedded_exif_gps_is_extracted(self):
        gps = extract_gps_from_jpeg_base64(jpeg_base64_with_gps())
        self.assertIsNotNone(gps)
        self.assertAlmostEqual(gps[0], -(2 + 16 / 60), places=6)
        self.assertAlmostEqual(gps[1], 40.9, places=6)

    def test_missing_and_out_of_range_gps_are_rejected(self):
        no_gps = base64.b64encode(JPEG_BASE64.encode()).decode("ascii")
        self.assertIsNone(extract_gps_from_jpeg_base64(no_gps))
        self.assertIsNone(
            gps_from_ifd(
                {1: "N", 2: (91, 0, 0), 3: "E", 4: (40, 0, 0)}
            )
        )

    def test_metadata_is_removed_before_lm_studio_payload(self):
        original = jpeg_base64_with_gps()
        stripped = strip_jpeg_metadata_base64(original)
        self.assertIsNotNone(extract_gps_from_jpeg_base64(original))
        self.assertIsNone(extract_gps_from_jpeg_base64(stripped))
        with Image.open(BytesIO(base64.b64decode(stripped))) as image:
            self.assertEqual(image.size, (2, 2))

        request = ReviewRequest.model_validate(
            request_data(image_base64=original)
        )
        payload = _build_payload(request, LMStudioSettings())
        image_url = payload["messages"][1]["content"][1]["image_url"]["url"]
        outgoing = image_url.partition(",")[2]
        self.assertIsNone(extract_gps_from_jpeg_base64(outgoing))


class ReverseGeocodingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clear_reverse_geocode_cache()
        self.settings = GeocodingSettings(minimum_interval_seconds=1)

    async def test_nominatim_result_is_mapped_and_cached(self):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "address": {
                "country": "Kenya",
                "state": "Lamu County",
                "town": "Lamu",
                "neighbourhood": "Lamu Old Town",
            }
        }
        client = MagicMock()
        client.get = AsyncMock(return_value=response)
        context_manager = MagicMock()
        context_manager.__aenter__ = AsyncMock(return_value=client)
        context_manager.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "location_context.httpx.AsyncClient",
            return_value=context_manager,
        ):
            first = await reverse_geocode_gps(-2.266667, 40.9, self.settings)
            second = await reverse_geocode_gps(-2.266667, 40.9, self.settings)

        self.assertEqual(first.status, "ok")
        self.assertEqual(first.context.country, "Kenya")
        self.assertEqual(first.context.state_or_province, "Lamu County")
        self.assertEqual(first.context.city, "Lamu")
        self.assertEqual(first.context.sublocation, "Lamu Old Town")
        self.assertEqual(second.status, "cache_hit")
        client.get.assert_awaited_once()

    async def test_catalog_values_override_reverse_geocoder(self):
        geocoded = LocationContext(
            country="Kenya",
            state_or_province="Lamu County",
            city="Lamu",
            sublocation="Mkomani",
        )
        with patch(
            "location_context.reverse_geocode_gps",
            new=AsyncMock(return_value=ReverseGeocodeResult(geocoded, "ok")),
        ):
            resolved = await resolve_location_context(
                jpeg_base64_with_gps(),
                LocationContext(city="Lamu old town"),
                self.settings,
            )

        self.assertEqual(
            resolved.source, "lightroom_catalog+gps_reverse_geocode"
        )
        self.assertEqual(resolved.context.city, "Lamu old town")
        self.assertEqual(resolved.context.country, "Kenya")

    async def test_complete_catalog_context_skips_network(self):
        catalog = LocationContext(
            country="Kenya",
            state_or_province="Lamu",
            city="Lamu old town",
            sublocation="Old Town",
        )
        reverse = AsyncMock()
        with patch("location_context.reverse_geocode_gps", new=reverse):
            resolved = await resolve_location_context(
                jpeg_base64_with_gps(), catalog, self.settings
            )
        reverse.assert_not_awaited()
        self.assertEqual(resolved.context, catalog)
        self.assertEqual(resolved.reverse_geocoder_status, "not_needed")

    async def test_missing_gps_and_geocoder_error_are_nonfatal(self):
        no_gps = await resolve_location_context(
            JPEG_BASE64, None, self.settings
        )
        self.assertIsNone(no_gps.context)
        self.assertEqual(no_gps.reverse_geocoder_status, "not_run")

        with patch(
            "location_context.reverse_geocode_gps",
            new=AsyncMock(
                return_value=ReverseGeocodeResult(
                    None, "error", "ConnectTimeout"
                )
            ),
        ):
            failed = await resolve_location_context(
                jpeg_base64_with_gps(), None, self.settings
            )
        self.assertIsNone(failed.context)
        self.assertEqual(failed.reverse_geocoder_status, "error")


class LightroomLocationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin_main = PLUGIN_MAIN.read_text(encoding="utf-8")
        cls.plugin_client = PLUGIN_CLIENT.read_text(encoding="utf-8")

    def test_supported_textual_metadata_keys_and_mapping_are_used(self):
        for metadata_key in ("country", "stateProvince", "city", "location"):
            self.assertIn(f'metadataKey = "{metadata_key}"', self.plugin_main)
        self.assertIn(
            '{ requestKey = "sublocation", metadataKey = "location" }',
            self.plugin_main,
        )

    def test_optional_metadata_failure_warns_and_review_continues(self):
        self.assertIn("pcall(function()", self.plugin_main)
        self.assertIn("Utils.logger:warn", self.plugin_main)
        self.assertIn("locationContext", self.plugin_main)
        self.assertIn("AiClient.reviewPreview", self.plugin_main)

    def test_location_object_is_conditionally_transmitted(self):
        self.assertIn("if locationContext then", self.plugin_client)
        self.assertIn("body.location_context = locationContext", self.plugin_client)

    def test_preview_preserves_embedded_location_metadata(self):
        self.assertIn("LR_minimizeEmbeddedMetadata = false", self.plugin_main)
        self.assertIn("LR_removeLocationMetadata = false", self.plugin_main)
        self.assertIn('LR_embeddedMetadataOption = "all"', self.plugin_main)


if __name__ == "__main__":
    unittest.main()
