"""Extension singletons, kept separate so models can import ``db`` without
importing the application factory (and creating a cycle)."""

from __future__ import annotations

from flask_cors import CORS
from flask_jwt_extended import JWTManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy

from app.models.base import Base

db = SQLAlchemy(model_class=Base)
migrate = Migrate()
jwt = JWTManager()
cors = CORS()
