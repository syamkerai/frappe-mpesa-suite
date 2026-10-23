import importlib

from .abstract import B2CConnector

CONNECTOR_MAP = {
    "Mpesa": "mpsa_suite.connectors.mpesa.b2c_connector:MpesaB2CConnector",
}


def get_b2c_connector(provider: str, settings_name: str) -> B2CConnector:
    path = CONNECTOR_MAP.get(provider)
    if not path:
        raise ValueError(f"No connector registered for {provider}")

    module_name, cls_name = path.split(":")
    module = importlib.import_module(module_name)
    cls = getattr(module, cls_name)
    return cls(settings_name)
