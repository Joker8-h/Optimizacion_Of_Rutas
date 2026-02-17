from __future__ import annotations

from typing import Any, Dict, List, Literal
import os

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, conint, confloat

# -------------------------
# Configuración
# -------------------------
Preference = Literal["FASTEST", "LOW_FUEL", "CHEAPEST", "SHORT_DISTANCE"]

OSRM_BASE_URL = os.getenv("OSRM_BASE_URL", "https://osrm-popayan-production.up.railway.app")

DEFAULT_FUEL_L_PER_100KM = float(os.getenv("FUEL_L_PER_100KM", "7.5"))
DEFAULT_FUEL_PRICE_PER_LITER = int(os.getenv("FUEL_PRICE_PER_LITER", "15000"))  # COP


# -------------------------
# Models
# -------------------------
class LatLng(BaseModel):
    lat: confloat(ge=-90, le=90)
    lng: confloat(ge=-180, le=180)


class VehicleConfig(BaseModel):
    fuel_l_per_100km: confloat(gt=0, le=50) = Field(default=DEFAULT_FUEL_L_PER_100KM)


class RouteOptionsRequest(BaseModel):
    origin: LatLng
    destination: LatLng
    preference: Preference = "FASTEST"
    k: conint(ge=1, le=5) = 3
    fuel_price_per_liter: conint(ge=0, le=200000) = Field(default=DEFAULT_FUEL_PRICE_PER_LITER)
    vehicle: VehicleConfig = Field(default_factory=VehicleConfig)


class RouteOption(BaseModel):
    id: str
    distance_km: float
    duration_min: float
    fuel_liters: float
    fuel_cost_cop: float
    score: float
    geojson: Dict[str, Any]


class RouteOptionsResponse(BaseModel):
    preference: Preference
    requested: int
    returned: int
    routes: List[RouteOption]


# -------------------------
# Helpers
# -------------------------
def estimate_fuel(distance_km: float, fuel_l_per_100km: float) -> float:
    return (distance_km * fuel_l_per_100km) / 100.0


def score_route(
    pref: Preference,
    distance_km: float,
    duration_min: float,
    fuel_liters: float,
    fuel_cost: float,
) -> float:
    if pref == "FASTEST":
        return duration_min
    if pref == "SHORT_DISTANCE":
        return distance_km
    if pref == "LOW_FUEL":
        return fuel_liters
    if pref == "CHEAPEST":
        return fuel_cost
    return duration_min


def osrm_get_routes(origin: LatLng, dest: LatLng) -> Dict[str, Any]:
    url = f"{OSRM_BASE_URL}/route/v1/driving/{origin.lng},{origin.lat};{dest.lng},{dest.lat}"
    params = {
        "alternatives": "true",
        "overview": "full",
        "geometries": "geojson",
        "steps": "false",
    }

    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        return r.json()

    except requests.ConnectionError:
        raise HTTPException(
            status_code=502,
            detail=f"No hay conexión con OSRM en {OSRM_BASE_URL}. Verifica que OSRM esté activo.",
        )
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"OSRM error: {str(e)}")


# -------------------------
# App
# -------------------------
app = FastAPI(
    title="Route Options API",
    version="1.0.2",
)

# ✅ CORS correcto para React
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -------------------------
# Endpoints
# -------------------------
@app.get("/health")
def health():
    return {
        "ok": True,
        "api": "Route Options API",
        "osrm_base_url": OSRM_BASE_URL,
    }


@app.get("/osrm-test")
def osrm_test():
    # Coordenadas de prueba en Popayán
    origin = LatLng(lat=2.4448, lng=-76.6147)
    dest = LatLng(lat=2.4550, lng=-76.5980)

    data = osrm_get_routes(origin, dest)
    return {
        "ok": True,
        "routes_found": len(data.get("routes", [])),
    }


@app.post("/route-options", response_model=RouteOptionsResponse)
def route_options(req: RouteOptionsRequest):
    data = osrm_get_routes(req.origin, req.destination)
    routes = data.get("routes", [])

    if not routes:
        raise HTTPException(status_code=404, detail="No se encontraron rutas")

    out: List[RouteOption] = []

    for idx, rt in enumerate(routes[: req.k], start=1):
        dist_km = rt["distance"] / 1000.0
        dur_min = rt["duration"] / 60.0

        fuel_l = estimate_fuel(dist_km, req.vehicle.fuel_l_per_100km)
        fuel_cost = fuel_l * req.fuel_price_per_liter
        sc = score_route(req.preference, dist_km, dur_min, fuel_l, fuel_cost)

        out.append(
            RouteOption(
                id=f"r{idx}",
                distance_km=round(dist_km, 3),
                duration_min=round(dur_min, 1),
                fuel_liters=round(fuel_l, 3),
                fuel_cost_cop=round(fuel_cost, 0),
                score=float(sc),
                geojson=rt["geometry"],
            )
        )

    out.sort(key=lambda x: x.score)

    return RouteOptionsResponse(
        preference=req.preference,
        requested=req.k,
        returned=len(out),
        routes=out,
    )
