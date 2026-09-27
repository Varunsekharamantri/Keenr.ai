"""
Vendor / technology entity normalization for the graph view.

The extractor writes whatever the source text said, so the same vendor arrives
under several spellings — "Azure", "Microsoft Azure"; "AWS", "Amazon Web
Services"; "Google Cloud", "GCP", "Google". Without canonicalization the graph
splits one vendor into several nodes and undercounts every one of them.
"""
from typing import Dict, List, Optional

# Alias (lowercased) -> canonical vendor name.
_ALIASES: Dict[str, str] = {
    "aws": "AWS",
    "amazon web services": "AWS",
    "amazon": "AWS",
    "azure": "Microsoft Azure",
    "microsoft azure": "Microsoft Azure",
    "ms azure": "Microsoft Azure",
    "google cloud": "Google Cloud",
    "gcp": "Google Cloud",
    "google cloud platform": "Google Cloud",
    "google": "Google Cloud",
    "oci": "Oracle Cloud",
    "oracle cloud": "Oracle Cloud",
    "oracle": "Oracle",
    "openai": "OpenAI",
    "open ai": "OpenAI",
    "chatgpt": "OpenAI",
    "anthropic": "Anthropic",
    "claude": "Anthropic",
    "snowflake": "Snowflake",
    "databricks": "Databricks",
    "microsoft": "Microsoft",
    "salesforce": "Salesforce",
    "sap": "SAP",
    "servicenow": "ServiceNow",
    "workday": "Workday",
    "kubernetes": "Kubernetes",
    "k8s": "Kubernetes",
    "docker": "Docker",
    "terraform": "Terraform",
    "ibm": "IBM",
    "red hat": "Red Hat",
    "vmware": "VMware",
    "crowdstrike": "CrowdStrike",
    "palo alto networks": "Palo Alto Networks",
    "splunk": "Splunk",
    "datadog": "Datadog",
    "mongodb": "MongoDB",
    "kafka": "Apache Kafka",
    "apache kafka": "Apache Kafka",
    "nvidia": "NVIDIA",
    "accenture": "Accenture",
    "infosys": "Infosys",
    "tcs": "TCS",
    "tata consultancy services": "TCS",
    "cognizant": "Cognizant",
    "capgemini": "Capgemini",
    "deloitte": "Deloitte",
}

VENDOR_CATEGORY: Dict[str, str] = {
    "AWS": "Hyperscaler",
    "Microsoft Azure": "Hyperscaler",
    "Google Cloud": "Hyperscaler",
    "Oracle Cloud": "Hyperscaler",
    "Snowflake": "Data Platform",
    "Databricks": "Data Platform",
    "MongoDB": "Data Platform",
    "Apache Kafka": "Data Platform",
    "OpenAI": "AI",
    "Anthropic": "AI",
    "NVIDIA": "AI Infrastructure",
    "SAP": "Enterprise Apps",
    "Salesforce": "Enterprise Apps",
    "ServiceNow": "Enterprise Apps",
    "Workday": "Enterprise Apps",
    "Oracle": "Enterprise Apps",
    "Microsoft": "Enterprise Apps",
    "CrowdStrike": "Security",
    "Palo Alto Networks": "Security",
    "Splunk": "Security & Observability",
    "Datadog": "Security & Observability",
    "Kubernetes": "Platform Engineering",
    "Docker": "Platform Engineering",
    "Terraform": "Platform Engineering",
    "Red Hat": "Platform Engineering",
    "VMware": "Platform Engineering",
    "IBM": "Services & Infrastructure",
    "Accenture": "Systems Integrator",
    "Infosys": "Systems Integrator",
    "TCS": "Systems Integrator",
    "Cognizant": "Systems Integrator",
    "Capgemini": "Systems Integrator",
    "Deloitte": "Systems Integrator",
}


def normalize_entity(raw: Optional[str]) -> Optional[str]:
    """Canonical vendor name, or None if the value isn't usable."""
    if not raw:
        return None
    cleaned = str(raw).strip().strip(".,;:'\"")
    if len(cleaned) < 2:
        return None
    return _ALIASES.get(cleaned.lower(), cleaned)


def category_for(vendor: str) -> str:
    return VENDOR_CATEGORY.get(vendor, "Other")


def normalize_entities(raw_list: Optional[List]) -> List[str]:
    """Normalize a key_entities list, de-duplicated, order preserved."""
    out: List[str] = []
    for item in raw_list or []:
        name = normalize_entity(item)
        if name and name not in out:
            out.append(name)
    return out
