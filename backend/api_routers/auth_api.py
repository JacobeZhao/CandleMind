from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr
from backend.services.auth_service import AuthService

auth_router = APIRouter()
auth: AuthService | None = None


def init_auth(service: AuthService):
    global auth
    auth = service


# ==========
# Schemas
# ==========
class RegisterSchema(BaseModel):
    username: str
    email: EmailStr
    password: str


class LoginSchema(BaseModel):
    email: EmailStr
    password: str


# ==========
# API
# ==========

@auth_router.post("/register")
def register(data: RegisterSchema):
    if not auth or not auth.ready:
        raise HTTPException(503, "auth not ready")

    try:
        auth.register(
            username=data.username,
            email=data.email,
            password=data.password,
        )
        return {"msg": "register success"}
    except ValueError as e:
        raise HTTPException(400, str(e))


@auth_router.post("/login")
def login(data: LoginSchema):
    if not auth or not auth.ready:
        raise HTTPException(503, "auth not ready")

    try:
        token = auth.login(
            email=data.email,
            password=data.password,
        )
        return {
            "access_token": token,
            "token_type": "bearer",
        }
    except ValueError as e:
        raise HTTPException(401, str(e))


@auth_router.get("/profile")
def profile(token: str):
    if not auth or not auth.ready:
        raise HTTPException(503, "auth not ready")

    try:
        user = auth.verify_token(token)
        return {
            "id": str(user["_id"]),
            "username": user["username"],
            "email": user["email"],
            "created_at": user["created_at"],
        }
    except ValueError as e:
        raise HTTPException(401, str(e))

@auth_router.get("/")
def health():
    return {
        "status": "ok",
        "ready": auth.ready if auth else False,
    }