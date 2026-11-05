"""Example trusted administrator File policy; not a malware scanner."""
from app.models.write_plugin_models import PluginWarning, WriteDecision
from app.protocols.file_plugin_protocol import FilePlugin


async def before(request):
    if request.proposed and request.proposed.mime_type == "application/x-msdownload":
        return WriteDecision(outcome="reject", message="This deployment does not accept executables.")
    if request.content_availability == "submitted" and request.content is not None:
        if len(request.content) > 5 * 1024 * 1024:
            return WriteDecision(outcome="warn", message="Large attachment; review storage usage.")
    return WriteDecision(outcome="allow")


async def after(request):
    if request.operation == "delete":
        return PluginWarning(message="File deleted; review downstream references.", code="FILE_REFERENCES")
    return None


plugin = FilePlugin(
    before_save={"create": before, "update": before},
    after_save={"delete": after},
)
