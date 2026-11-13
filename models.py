import warnings

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import BigInteger, Boolean, Column, DateTime, false, ForeignKey, func, MetaData, String
from sqlalchemy.dialects.postgresql import JSONB

naming_convention = {
    "ix": 'ix_%(column_0_label)s',
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s"
}
db = SQLAlchemy(metadata=MetaData(naming_convention=naming_convention))


class Customer(db.Model):
    """
    "amo_customer": {
        "general": {
            "id": int,
            "login": str,
            "billing_type": str,
            "is_robot": bool,
            "is_nds": bool,
            "system": str,
            "host": str,
            "name": str
        },
        "api_value": {}
    }

    "amo_companies": [
        {
            "id": int,
            "inn": Optional[str],
            "name": str
        }
    ]

    "send_methods": [
        {
            "type": str (mail,sms,wtsp),
            "value": str,
            "desc": str,
        }
    ]
    """
    __tablename__ = "customer"
    id = Column(BigInteger, nullable=False, primary_key=True)
    external_id = Column(BigInteger, nullable=True, unique=True)  # Customer id from amoCRM
    login = Column(String(255), nullable=False, unique=True)

    # Is row marked for delete
    to_delete = Column(Boolean, nullable=False, default=False, server_default=false())
    amo_customer = Column(JSONB(none_as_null=True))
    amo_companies = Column(JSONB(none_as_null=True))
    send_methods = Column(JSONB(none_as_null=True))
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, onupdate=func.now())


class Lead(db.Model):
    """
    "amo_lead": {
        "general": {
            "id": int,
            "login": Optional[str],
            "name": str
        },
        "api_value": {}
    }

    "amo_contacts": [
        {
            "id": int,
            "phone": Optional[str],
            "name": str
        }
    ]
    """
    __tablename__ = "lead"
    id = Column(BigInteger, nullable=False, primary_key=True)
    external_id = Column(BigInteger, nullable=False, unique=True)  # Lead id from amoCRM
    login = Column(String(255), index=True)
    customer_id = Column(BigInteger,
                         ForeignKey("customer.id", ondelete="SET NULL"),
                         nullable=True, index=True)

    # Is row marked for delete
    to_delete = Column(Boolean, nullable=False, default=False, server_default=false())
    amo_lead = Column(JSONB(none_as_null=True))
    amo_contacts = Column(JSONB(none_as_null=True))
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, onupdate=func.now())


@warnings.deprecated("Запись происходит в таблицы Customer и Lead")
class AbstractCustomer(db.Model):
    """
    "default" "general" : {
        "id": int
    }

    "amo_customers": [
        {
            "general": {
                "login": str,
                "billing_type": str,
                "is_robot": bool,
                "is_nds": bool,
                "system": str,
                "host": str,
                "name": str
            },
            "api_value": {}
        }
    ]

    "amo_leads": [
        {
            "general": {
                "login": Optional[str],
                "name": str
            }
            "api_value": {}
        }
    ]

    "amo_companies": [
        {
            "general": {
                "id": int,
                "inn": Optional[str],
                "name": str
            }
            "api_value": {}
        }
    ]

    "amo_contacts": [
        {
            "general": {
                "id": int,
                "phone": Optional[str],
                "name": str
            }
            "api_value": {}
        }
    ]

    "send_methods": [
        {
            "type": str (mail,sms,wtsp),
            "value": str,
            "desc": str,
        }
    ]
    """
    __tablename__ = "abstract_customer"
    id = Column(db.BigInteger(), nullable=False, primary_key=True)
    amo_customers = Column(JSONB)
    amo_leads = Column(JSONB)
    amo_companies = Column(JSONB)
    amo_contacts = Column(JSONB)
    send_methods = Column(JSONB)
    created_at = Column(db.DateTime, server_default=func.now())
    updated_at = Column(db.DateTime, onupdate=func.now())
