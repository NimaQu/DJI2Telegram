from qdc507_gateway.events import EventBus
from qdc507_gateway.modem.service import LiveModuleService
from qdc507_gateway.storage.database import Database
from qdc507_gateway.usb.descriptors import LibUSBDeviceLocator


def test_live_locator_filters_configured_identity():
    class Device:
        def __init__(self, vid, pid):
            self.vid, self.pid = vid, pid
        def getVendorID(self):
            return self.vid
        def getProductID(self):
            return self.pid
        def iterSettings(self):
            return ()
        def getBusNumber(self):
            return 1
        def getDeviceAddress(self):
            return 2

    class Context:
        def getDeviceList(self, **kwargs):
            return (Device(0x2C7C, 0x0125), Device(0x2CA3, 0x4006))

    locator = LibUSBDeviceLocator(context=Context(), vendor_id=0x2CA3, product_id=0x4006)
    assert [device.identity for device in locator.find()] == ["2CA3:4006"]
    assert [device.identity for device in locator.find(0x2C7C, 0x0125)] == ["2C7C:0125"]


def test_service_routes_identity_to_session_monitor_and_status(tmp_path):
    state = {}
    database = Database(":memory:")
    service = LiveModuleService(
        database, EventBus(), lock_path=tmp_path / "owner.lock", state=state,
        vendor_id=0x2CA3, product_id=0x4006,
    )
    try:
        session = service._session()
        try:
            assert (session.vendor_id, session.product_id) == (0x2CA3, 0x4006)
        finally:
            session.close()
        if service._udev_monitor is not None:
            assert service._udev_monitor.vendor_id == "2ca3"
            assert service._udev_monitor.product_id == "4006"
        service._set_connection_state(True)
        assert state["module"]["identity"] == "2CA3:4006"
    finally:
        service.close()
        database.close()
