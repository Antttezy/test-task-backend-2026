import logging
from collections import defaultdict
from typing import Dict, List

from sqlalchemy import BigInteger, String, Values, cast, column, func, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert

from amocrm import Customer as AmoCustomer
from amocrm import Lead as AmoLead
from models import Customer, Lead, db

logger = logging.getLogger(__name__)
JSONB_NULL = JSONB(none_as_null=True)


def get_customer_json(customer: AmoCustomer) -> dict:
    return {
        'general': {
            'id': customer.id,
            'login': customer.custom_fields_values.get('Логин', {}).get('value'),
            'billing_type': customer.custom_fields_values.get('Вид оплаты', {}).get('value'),
            'is_robot': bool(customer.custom_fields_values.get('Робот')),
            'is_nds': bool(customer.custom_fields_values.get('Оплата с НДС')),
            'system': customer.custom_fields_values.get('Система', {}).get('value'),
            'host': customer.custom_fields_values.get('Хост', {}).get('value'),
            'name': customer.name,
        },
        'api_value': customer.as_dict()
    }


def get_lead_json(lead: AmoLead) -> dict:
    return {
        'general': {
            'id': lead.id,
            'login': lead.custom_fields_values.get('Логин', {}).get('value'),
            'name': lead.name,
        },
        'api_value': lead.as_dict()
    }


def _jsonb_merge(column_, value):
    return func.coalesce(column_, func.jsonb_build_object())\
        .op('||')(func.coalesce(value, func.jsonb_build_object()))


def _merge_companies(target, source) -> List[dict]:
    target = target or []
    seen = {c.get('id') for c in target}
    return target + [c for c in (source or []) if c.get('id') not in seen]


def _values(alias: str, spec, rows: List[Dict]):
    cols = [column(name, type_) for name, type_ in spec]
    names = [name for name, _ in spec]
    types = dict(spec)
    data = [
        [r[name] if r[name] is not None else cast(None, types[name]) for name in names]
        for r in rows
    ]
    return Values(*cols, name=alias).data(data)


def upsert_customers(rows: List[Dict]) -> None:
    """
    Batch upsert покупателей

    :param rows: [{'external_id', 'login', 'amo_customer', 'amo_companies'}]

    Логика:
    Нашли по external_id -> merge jsonb, to_delete=false.
    Иначе ищем по логину среди сохраненных сделок: merge jsonb, to_delete=false.
    Если по логину нашлось больше одной записи - совпадение не используется.
    Иначе INSERT нового покупателя; при конфликте по логину слияние в существующую строку.
    """
    if not rows:
        return
    session = db.session
    by_external = {r['external_id']: r for r in rows}

    # Строки, уже существующие в БД
    found_external_ids = set(session.scalars(
        select(Customer.external_id).where(Customer.external_id.in_(list(by_external)))
    ).all())

    # Обновление существующих по external_id
    if found_external_ids:
        v = _values('v', [
            ('external_id', BigInteger),
            ('login', String),
            ('amo_customer', JSONB_NULL),
            ('amo_companies', JSONB_NULL),
        ], [by_external[e] for e in found_external_ids])
        session.execute(
            update(Customer)
            .where(Customer.external_id == v.c.external_id)
            .values(
                login=v.c.login,
                amo_customer=_jsonb_merge(Customer.amo_customer, v.c.amo_customer),
                amo_companies=v.c.amo_companies,
                to_delete=False,
            ),
            execution_options={"synchronize_session": None},
        )

    # Поиск по логину среди уже сохраненных сделок
    remaining = [r for e, r in by_external.items() if e not in found_external_ids]
    if not remaining:
        return

    by_login = {r['login']: r for r in remaining}
    lead_rows = session.execute(
        select(Lead.login, Lead.customer_id).where(
            Lead.login.in_(list(by_login)),
            Lead.customer_id.isnot(None),
        )
    ).all()

    login_to_customers: Dict[str, set] = defaultdict(set)
    for login, customer_id in lead_rows:
        login_to_customers[login].add(customer_id)

    matched, to_insert = [], []
    for login, row in by_login.items():
        customer_ids = login_to_customers.get(login) or set()
        if len(customer_ids) == 1:  # Если больше 1 записи, совпадение не используется
            matched.append({**row, 'id': customer_ids.pop()})
        else:
            to_insert.append(row)

    if matched:
        # Обновление из сохраненных сделок
        v = _values('v', [
            ('id', BigInteger),
            ('external_id', BigInteger),
            ('amo_customer', JSONB_NULL),
            ('amo_companies', JSONB_NULL),
        ], matched)
        session.execute(
            update(Customer)
            .where(Customer.id == v.c.id)
            .values(
                external_id=v.c.external_id,
                amo_customer=_jsonb_merge(Customer.amo_customer, v.c.amo_customer),
                amo_companies=v.c.amo_companies,
                to_delete=False,
            ),
            execution_options={"synchronize_session": None},
        )

    # Новые покупатели + слияние при конфликте по логину
    if to_insert:
        stmt = pg_insert(Customer).values(to_insert)
        stmt = stmt.on_conflict_do_update(
            index_elements=['login'],
            set_={
                'amo_customer': _jsonb_merge(Customer.amo_customer, stmt.excluded.amo_customer),
                'external_id': stmt.excluded.external_id,
                'to_delete': False,
            },
        )
        session.execute(stmt)


def upsert_leads(rows: List[Dict]) -> None:
    """
    Batch upsert сделок

    :param rows: [{'external_id', 'login', 'amo_lead', 'amo_contacts'}]

    Логика:
    Сделка нашлась в БД по external_id -> перезаписываем jsonb, to_delete=false.
    Покупатель при этом может смениться:
    - покупателя не было (customer_id=NULL), а покупатель с логином сделки
      нашелся -> привязываем его к сделке;
    - покупатель у сделки есть, но это не тот, кто нашелся по логину
      (A != B) -> сливаем в A (запись, найденную по сделке): лиды B
      перепривязываем к A, jsonb B мержим в A, списки компаний объединяем
      без дублей, B помечаем to_delete=true.
    Сделки в БД нет -> создаем новую. Покупатель ищется по точному логину:
    нашелся -> привязываем, to_delete=false, не нашелся -> customer_id=NULL, to_delete=true
    """
    if not rows:
        return
    session = db.session
    by_external = {r['external_id']: r for r in rows}

    found = session.execute(
        select(Lead.external_id, Lead.customer_id).where(
            Lead.external_id.in_(list(by_external)))
    ).all()
    found_by_external = {f.external_id: f for f in found}

    # Родители по точному логину
    logins = {r['login'] for r in rows if r['login']}
    customer_by_login = {}
    if logins:
        customer_by_login = dict(session.execute(
            select(Customer.login, Customer.id).where(
                Customer.login.in_(logins),
                Customer.to_delete.is_(False),
            )
        ).all())

    updates, inserts = [], []
    merge_sources: Dict[int, int] = {}

    for external_id, row in by_external.items():
        login = row['login']
        parent_id = customer_by_login.get(login)
        existing = found_by_external.get(external_id)
        if existing is not None:
            update_row = {
                'external_id': external_id,
                'amo_lead': row['amo_lead'],
                'amo_contacts': row['amo_contacts'],
                'login': login,
                'customer_id': existing.customer_id,
            }
            if parent_id is not None:
                if existing.customer_id is None:
                    update_row['customer_id'] = parent_id  # Ссылке без родителя укажем покупателя
                elif existing.customer_id != parent_id:
                    # Сделка нашлась у A, по логину - B: сливаемся в ту, что по сделке
                    merge_sources[parent_id] = existing.customer_id
            updates.append(update_row)
        else:
            inserts.append({
                'external_id': external_id,
                'login': login,
                'customer_id': parent_id,
                'amo_lead': row['amo_lead'],
                'amo_contacts': row['amo_contacts'],
                'to_delete': parent_id is None,
            })

    if updates:
        v = _values('v', [
            ('external_id', BigInteger),
            ('amo_lead', JSONB_NULL),
            ('amo_contacts', JSONB_NULL),
            ('login', String),
            ('customer_id', BigInteger),
        ], updates)
        session.execute(
            update(Lead)
            .where(Lead.external_id == v.c.external_id)
            .values(
                amo_lead=v.c.amo_lead,
                amo_contacts=v.c.amo_contacts,
                login=v.c.login,
                customer_id=v.c.customer_id,
                to_delete=False,
            ),
            execution_options={"synchronize_session": None},
        )

    if inserts:
        session.execute(pg_insert(Lead), inserts)

    # Слияние покупателей по сделке без обращений к БД в цикле
    if merge_sources:
        merge_ids = list(merge_sources) + list(merge_sources.values())
        customers_by_id = {
            c.id: c for c in session.scalars(
                select(Customer).where(Customer.id.in_(merge_ids))).all()
        }
        pair_map = {s: t for s, t in merge_sources.items() if s != t}
        sources_by_target: Dict[int, list] = defaultdict(list)
        for source_id, target_id in pair_map.items():
            sources_by_target[target_id].append(source_id)

        reassign, merge_rows, source_ids = [], [], []

        for target_id, src_ids in sources_by_target.items():
            target = customers_by_id.get(target_id)
            if target is None:
                continue
            amo_customer, companies = target.amo_customer, target.amo_companies
            external_id = target.external_id
            for source_id in src_ids:
                source = customers_by_id.get(source_id)
                if source is None:
                    continue
                logger.info(f"Слияние покупателей: {source_id} -> {target_id}")
                reassign.append({'source_id': source_id, 'target_id': target_id})
                source_ids.append(source_id)
                amo_customer = {**(amo_customer or {}), **(source.amo_customer or {})}  # Merge
                companies = _merge_companies(companies, source.amo_companies)
                external_id = external_id or source.external_id
            merge_rows.append({
                'target_id': target_id,
                'amo_customer': amo_customer,
                'amo_companies': companies,
                'external_id': external_id,
            })

        v = _values('v', [('source_id', BigInteger), ('target_id', BigInteger)], reassign)
        session.execute(
            update(Lead)
            .where(Lead.customer_id == v.c.source_id)
            .values(customer_id=v.c.target_id),
            execution_options={"synchronize_session": None},
        )

        v = _values('v', [
            ('target_id', BigInteger),
            ('amo_customer', JSONB_NULL),
            ('amo_companies', JSONB_NULL),
            ('external_id', BigInteger),
        ], merge_rows)
        session.execute(
            update(Customer)
            .where(Customer.id == v.c.target_id)
            .values(
                amo_customer=v.c.amo_customer,
                amo_companies=v.c.amo_companies,
                external_id=v.c.external_id,
                to_delete=False,
            ),
            execution_options={"synchronize_session": None},
        )
        # Источники, не являющиеся целью другой пары
        sources = list(set(source_ids)-set(sources_by_target))
        session.execute(
            update(Customer).where(Customer.id.in_(sources)).values(to_delete=True),
            execution_options={"synchronize_session": None},
        )
