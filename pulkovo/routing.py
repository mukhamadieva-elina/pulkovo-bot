"""Routing decisions shared by VK processing and the admin preview."""

STAFF_REASONS = {'operator_request', 'requires_staff', 'unresolved', 'unsafe', 'out_of_scope', 'complaint'}


def handoff_reason(decision, card=None):
    if card and card['action'] == 'handoff' and decision['action'] == 'answer':
        return 'card_requires_staff'
    if decision['action'] != 'handoff':
        return ''
    return {
        'operator_request': 'user_requested_operator',
        'requires_staff': 'staff_action_required',
        'unresolved': 'answer_not_helpful',
        'unsafe': 'unsafe_request',
        'out_of_scope': 'out_of_scope',
        'complaint': 'complaint',
    }.get(decision.get('handoff_reason'), 'no_matching_answer')


def needs_full_catalog(decision):
    return decision['action'] == 'handoff' and decision.get('handoff_reason') not in STAFF_REASONS
