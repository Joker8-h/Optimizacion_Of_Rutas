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
# Nuevos modelos para cobro por paradas
# -------------------------

class PassengerSegment(BaseModel):
    """
    Representa el tramo de un pasajero dentro de una ruta con paradas.
    - start_index: índice de la parada donde se sube (0 = primera parada)
    - end_index: índice de la parada donde se baja (debe ser > start_index)
    - passenger_id: identificador opcional para relacionarlo con tu sistema
    """

    passenger_id: Any | None = None
    start_index: conint(ge=0)
    end_index: conint(ge=1)


class SegmentFareRequest(BaseModel):
    """
    Request para calcular el valor a pagar de cada pasajero según las paradas.
    - stops: lista de paradas ordenadas de inicio a fin
    - total_route_price_cop: precio total que normalmente cobrarías por toda la ruta
    - passengers: lista de pasajeros con su tramo dentro de la ruta
    """

    stops: List[LatLng] = Field(min_items=2, description="Paradas ordenadas de la ruta")
    total_route_price_cop: conint(gt=0)
    passengers: List[PassengerSegment] = Field(min_items=1)


class PassengerFare(BaseModel):
    passenger_id: Any | None
    start_index: int
    end_index: int
    distance_km: float
    fare_cop: int


class SegmentFareResponse(BaseModel):
    total_distance_km: float
    total_route_price_cop: int
    passengers: List[PassengerFare]


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


def osrm_get_multi_route(stops: List[LatLng]) -> Dict[str, Any]:
    """
    Obtiene una ruta única pasando por todas las paradas en orden.
    Aprovecha que OSRM permite varios puntos:
    /route/v1/driving/long1,lat1;long2,lat2;long3,lat3;...
    """

    if len(stops) < 2:
        raise HTTPException(status_code=400, detail="Se requieren al menos 2 paradas")

    coords = ";".join(f"{s.lng},{s.lat}" for s in stops)
    url = f"{OSRM_BASE_URL}/route/v1/driving/{coords}"
    params = {
        "alternatives": "false",
        "overview": "false",
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
        "*"
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


@app.post("/segment-fares", response_model=SegmentFareResponse)
def segment_fares(req: SegmentFareRequest):
    """
    Calcula cuánto debe pagar cada pasajero según las paradas en las que se sube/baja.

    Lógica:
    1. Se calcula la distancia total de la ruta usando todas las paradas.
    2. Se obtiene la distancia entre cada par de paradas consecutivas (legs).
    3. Para cada pasajero se suma la distancia de los legs entre start_index y end_index.
    4. El valor que paga es proporcional a la distancia recorrida:
       fare = total_route_price_cop * (dist_pax / dist_total)
    """

    if len(req.stops) < 2:
        raise HTTPException(status_code=400, detail="Se requieren al menos 2 paradas")

    # Validar índices de pasajeros
    max_index = len(req.stops) - 1
    for p in req.passengers:
        if p.start_index < 0 or p.end_index <= p.start_index:
            raise HTTPException(
                status_code=400,
                detail="Cada pasajero debe tener start_index >= 0 y end_index > start_index",
            )
        if p.end_index > max_index:
            raise HTTPException(
                status_code=400,
                detail=f"Los índices de paradas deben estar entre 0 y {max_index}",
            )

    data = osrm_get_multi_route(req.stops)
    routes = data.get("routes", [])

    if not routes:
        raise HTTPException(status_code=404, detail="No se encontró ruta OSRM para las paradas dadas")

    route = routes[0]
    legs = route.get("legs")

    if not legs:
        raise HTTPException(
            status_code=502,
            detail="La respuesta de OSRM no contiene información de legs entre paradas",
        )

    # OSRM devuelve un leg por cada tramo entre paradas consecutivas
    # distance viene en metros
    leg_distances_km: List[float] = [(leg["distance"] or 0) / 1000.0 for leg in legs]

    total_distance_km = sum(leg_distances_km)
    if total_distance_km <= 0:
        raise HTTPException(
            status_code=502,
            detail="La distancia total calculada es 0, no se puede repartir el precio",
        )

    passenger_fares: List[PassengerFare] = []

    for p in req.passengers:
        # Sumar distancia entre start_index y end_index
        # Ejemplo: paradas [0,1,2,3], pasajero 1->3 => legs 1-2 y 2-3 => índices 1 y 2
        seg_distance_km = sum(
            leg_distances_km[i]
            for i in range(p.start_index, p.end_index)
            if 0 <= i < len(leg_distances_km)
        )

        proportion = seg_distance_km / total_distance_km
        fare = int(round(req.total_route_price_cop * proportion))

        passenger_fares.append(
            PassengerFare(
                passenger_id=p.passenger_id,
                start_index=p.start_index,
                end_index=p.end_index,
                distance_km=round(seg_distance_km, 3),
                fare_cop=fare,
            )
        )

    return SegmentFareResponse(
        total_distance_km=round(total_distance_km, 3),
        total_route_price_cop=req.total_route_price_cop,
        passengers=passenger_fares,
    )
