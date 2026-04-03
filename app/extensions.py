from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_mail import Mail
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFProtect

db = SQLAlchemy()
login_manager = LoginManager()
mail = Mail()
migrate = Migrate()
csrf = CSRFProtect()

login_manager.login_view = "auth.login"
login_manager.login_message = "Bitte melden Sie sich an."
login_manager.login_message_category = "warning"
