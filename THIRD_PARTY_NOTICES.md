# Third-party requirements and notices

The [MIT license](LICENSE) applies to original project code and documentation,
not external libraries, services, model weights, photographs, or Adobe software.

## Dependencies and runtime

[server/requirements.txt](server/requirements.txt) lists FastAPI, Uvicorn,
HTTPX, Pydantic, PyYAML, and Pillow. They and their transitive dependencies
retain their own licenses/notices. Requirements are ranges, not a locked list
of every installed component. Review the license metadata and bundled notices
for the versions you distribute.

Adobe Lightroom Classic is proprietary and separately licensed. Adobe and
Lightroom are Adobe trademarks. This project is independent and not endorsed
by Adobe, LM Studio, or stock agencies. No Lightroom binaries or model weights
are bundled.

LM Studio and models have separate licenses and hardware/runtime requirements.
Configured example model IDs do not guarantee image support or grant model
redistribution rights. A remote inference endpoint may have additional terms
and charges.

## Location and generated judgments

Nominatim/OpenStreetMap services and underlying location data have separate
usage and attribution requirements. Review the applicable terms for your
deployment and redistributed data. Setting a User-Agent or interval does not
grant unlimited service use. GPS can leave the machine for reverse lookup.

AI verdicts and editorial/release judgments are not stock-agency acceptance,
legal advice, or verified release clearance. Users must assess their photos and
submission rights independently. The maintainer is responsible for attribution
and rights to contributed/reused code; this notice does not override another
party's license or certify all third-party provenance.
