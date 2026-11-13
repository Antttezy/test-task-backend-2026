import logging
from typing import Iterator, List, Dict

from sqlalchemy import delete, or_, update

from abstract_customer import get_customer_json, get_lead_json, upsert_customers, upsert_leads
from amocrm import Customer, COMPANIES, CONTACTS, Lead, amocrm_get
from models import db, Customer as CustomerModel, Lead as LeadModel

logger = logging.getLogger(__name__)

BATCH_SIZE = 1000
DELETE_MARK = 'удалить'


def _is_marked_for_deletion(name) -> bool:
    return bool(name) and DELETE_MARK in name.lower()


def _flush_customers(batch: List[Dict]) -> None:
    try:
        upsert_customers(batch)
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception(f"Батч покупателей ({len(batch)} шт.) не записан")
        raise
    batch.clear()


def _flush_leads(batch: List[Dict]) -> None:
    try:
        upsert_leads(batch)
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception(f"Батч сделок ({len(batch)} шт.) не записан")
        raise
    batch.clear()


def collect_leads_customers(is_full=True):
    session = db.session
    if is_full:
        session.execute(update(CustomerModel).values(to_delete=True))
        session.execute(update(LeadModel).values(to_delete=True))
        session.commit()
        logger.info("to_delete=true перед синхронизацией")

    customers: Iterator[Customer] = amocrm_get(Customer, _with=f"{COMPANIES},{CONTACTS}")
    logger.info(f"Collected customers {is_full=}")

    batch: List[Dict] = []
    for customer in customers:
        if not is_full and customer.status_id == 111349:
            continue
        customer_login = customer.get_field('Логин')
        if not customer_login:
            continue  # покупатель без логина пропускается
        if _is_marked_for_deletion(customer.name):
            continue
        batch.append({
            'external_id': customer.id,
            'login': customer_login,
            'amo_customer': get_customer_json(customer),
            'amo_companies': customer.companies or None,
        })
        if len(batch) >= BATCH_SIZE:
            _flush_customers(batch)
    if batch:
        _flush_customers(batch)

    del customers

    amo_leads: Iterator[Lead] = amocrm_get(Lead, _with=f"{CONTACTS}")
    logger.info(f"Collected leads {is_full=}")
    batch = []
    for amo_lead in amo_leads:
        if not is_full and amo_lead.status_id == 143:
            continue
        if _is_marked_for_deletion(amo_lead.name):
            continue
        batch.append({
            'external_id': amo_lead.id,
            'login': amo_lead.get_field('Логин'),
            'amo_lead': get_lead_json(amo_lead),
            'amo_contacts': amo_lead.contacts or None,
        })
        if len(batch) >= BATCH_SIZE:
            _flush_leads(batch)
    if batch:
        _flush_leads(batch)

    if not is_full:
        return

    try:
        logger.info("Чистка устаревших сделок и покупателей")
        session.execute(delete(CustomerModel).where(CustomerModel.to_delete.is_(True)))
        session.execute(delete(LeadModel).where(or_(
            LeadModel.to_delete.is_(True),
            LeadModel.customer_id.is_(None),
        )))
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Чистка не выполнена")
