import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods

from .models import Vehicle
from . import analysis
from .services import ai


def parse_body(request) -> dict:
    try:
        return json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return {}


@require_GET
def health(request):
    return JsonResponse({"status": "ok"})


@require_GET
def actions(request):
    try:
        return JsonResponse(analysis.analyze())
    except FileNotFoundError as exc:
        return JsonResponse({"error": f"Data file missing: {exc.filename}"}, status=500)


@require_GET
def shift_brief(request):
    # Isolated from /api/actions: a failure here can never affect the ranking.
    from . import brief
    try:
        return JsonResponse(brief.build_brief())
    except Exception as exc:  # last-resort guard; UI hides the panel on error
        return JsonResponse({"error": f"brief unavailable: {exc}"}, status=500)


@require_GET
def ai_ping(request):
    return JsonResponse(ai.ping())


# Hackathon shortcut: csrf_exempt on JSON API views (no auth/cookies in scope).
@csrf_exempt
@require_http_methods(["GET", "POST"])
def vehicles(request):
    if request.method == "POST":
        body = parse_body(request)
        name = (body.get("name") or "").strip()
        if not name:
            return JsonResponse({"error": "name is required"}, status=400)
        v = Vehicle.objects.create(name=name, status=body.get("status", "active"))
        return JsonResponse(v.to_dict(), status=201)
    return JsonResponse({"results": [v.to_dict() for v in Vehicle.objects.order_by("id")]})
