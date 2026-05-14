from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class RestaurantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    address: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    latitude: Decimal | None = None
    longitude: Decimal | None = None
    menu_source_url: str | None = None


class RestaurantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    address: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None


class ParseMenuRequest(BaseModel):
    menu_file_path: str = Field(min_length=1)


class ParseMenuResponse(BaseModel):
    dishes_inserted: int
    ingredients_inserted: int
    cost_usd: Decimal


class IngredientOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    normalized_name: str
    quantity: Decimal | None = None
    unit: str | None = None
    estimation_confidence: float | None = None


class DishOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    description: str | None = None
    price: Decimal | None = None
    parse_confidence: float | None = None
    ingredients: list[IngredientOut] = Field(default_factory=list)
