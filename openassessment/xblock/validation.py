"""
Validate changes to an XBlock before it is updated.
"""
from collections import Counter
from django.utils.translation import ugettext as _
from openassessment.assessment.serializers import rubric_from_dict, InvalidRubric
from openassessment.assessment.api.student_training import validate_training_examples
from openassessment.xblock.resolve_dates import resolve_dates, DateValidationError, InvalidDateFormat
from openassessment.xblock.data_conversion import convert_training_examples_list_to_dict


def _match_by_order(items, others):
    """
    Given two lists of dictionaries, each containing "order_num" keys,
    return a set of tuples, where the items in the tuple are dictionaries
    with the same "order_num" keys.

    Args:
        items (list of dict): Items to match, each of which must contain a "order_num" key.
        others (list of dict): Items to match, each of which must contain a "order_num" key.

    Returns:
        list of tuples, each containing two dictionaries

    Raises:
        IndexError: A dictionary does no contain a 'order_num' key.
    """
    # Sort each dictionary by its "name" key, then zip them and return
    key_func = lambda x: x['order_num']
    return zip(sorted(items, key=key_func), sorted(others, key=key_func))


def _duplicates(items):
    """
    Given an iterable of items, return a set of duplicate items in the list.

    Args:
        items (list): The list of items, which may contain duplicates.

    Returns:
        set: The set of duplicate items in the list.

    """
    counts = Counter(items)
    return set(x for x in items if counts[x] > 1)


def _is_valid_assessment_sequence(assessments):
    """
    Check whether the sequence of assessments is valid.
    For example, we currently allow self-assessment after peer-assessment,
    but do not allow peer-assessment before self-assessment.

    Args:
        assessments (list of dict): List of assessment dictionaries.

    Returns:
        bool

    """
    valid_sequences = [
        ['self-assessment'],
        ['peer-assessment'],
        ['peer-assessment', 'self-assessment'],
        ['self-assessment', 'peer-assessment'],
        ['student-training', 'peer-assessment'],
        ['student-training', 'peer-assessment', 'self-assessment'],
        ['student-training', 'self-assessment', 'peer-assessment'],
        ['self-assessment', 'student-training', 'peer-assessment'],
        ['example-based-assessment'],
        ['example-based-assessment', 'self-assessment'],
        ['example-based-assessment', 'peer-assessment'],
        ['example-based-assessment', 'peer-assessment', 'self-assessment'],
        ['example-based-assessment', 'self-assessment', 'peer-assessment'],
        ['example-based-assessment', 'student-training', 'peer-assessment'],
        ['example-based-assessment', 'student-training', 'peer-assessment', 'self-assessment'],
        ['example-based-assessment', 'student-training', 'self-assessment', 'peer-assessment'],
        ['example-based-assessment', 'self-assessment', 'student-training', 'peer-assessment'],
    ]

    sequence = [asmnt.get('name') for asmnt in assessments]
    return sequence in valid_sequences


def validate_assessments(assessments, current_assessments, is_released):
    """
    Check that the assessment dict is semantically valid.

    Valid assessment steps are currently:
    * peer, then self
    * self only

    If a question has been released, the type and number of assessment steps
    cannot be changed.

    Args:
        assessments (list of dict): list of serialized assessment models.
        current_assessments (list of dict): list of the current serialized
            assessment models. Used to determine if the assessment configuration
            has changed since the question had been released.
        is_released (boolean) : True if the question has been released.

    Returns:
        tuple (is_valid, msg) where
            is_valid is a boolean indicating whether the assessment is semantically valid
            and msg describes any validation errors found.
    """
    if len(assessments) == 0:
        return (False, _("This problem must include at least one assessment."))

    # Ensure that we support this sequence of assessments.
    if not _is_valid_assessment_sequence(assessments):
        msg = _("The assessment order you selected is invalid.")
        return False, msg

    for assessment_dict in assessments:
        # Number you need to grade is >= the number of people that need to grade you
        if assessment_dict.get('name') == 'peer-assessment':
            must_grade = assessment_dict.get('must_grade')
            must_be_graded_by = assessment_dict.get('must_be_graded_by')

            if must_grade is None or must_grade < 1:
                return (False, _('The "must_grade" value must be a positive integer.'))

            if must_be_graded_by is None or must_be_graded_by < 1:
                return (False, _('The "must_be_graded_by" value must be a positive integer.'))

            if must_grade < must_be_graded_by:
                return (False, _('The "must_grade" value must be greater than or equal to the "must_be_graded_by" value.'))

        # Student Training must have at least one example, and all
        # examples must have unique answers.
        if assessment_dict.get('name') == 'student-training':
            answers = []
            examples = assessment_dict.get('examples')
            if not examples:
                return False, _('You must provide at least one example response for student training.')
            for example in examples:
                if example.get('answer') in answers:
                    return False, _('Each example response for student training must be unique.')
                answers.append(example.get('answer'))

        # Example-based assessment MUST specify 'ease' or 'fake' as the algorithm ID,
        # at least for now.  Later, we may make this more flexible.
        if assessment_dict.get('name') == 'example-based-assessment':
            if assessment_dict.get('algorithm_id') not in ['ease', 'fake']:
                return (False, _('The "algorithm_id" value must be set to "ease" or "fake"'))

    if is_released:
        if len(assessments) != len(current_assessments):
            return (False, _("The number of assessments cannot be changed after the problem has been released."))

        names = [assessment.get('name') for assessment in assessments]
        current_names = [assessment.get('name') for assessment in current_assessments]
        if names != current_names:
            return (False, _("The assessment type cannot be changed after the problem has been released."))

    return (True, u'')


def validate_rubric(rubric_dict, current_rubric, is_released, is_example_based):
    """
    Check that the rubric is semantically valid.

    Args:
        rubric_dict (dict): Serialized Rubric model representing the updated state of the rubric.
        current_rubric (dict): Serialized Rubric model representing the current state of the rubric.
        is_released (bool): True if and only if the problem has been released.
        is_example_based (bool): True if and only if this is an example-based assessment.

    Returns:
        tuple (is_valid, msg) where
            is_valid is a boolean indicating whether the assessment is semantically valid
            and msg describes any validation errors found.
    """
    try:
        rubric_from_dict(rubric_dict)
    except InvalidRubric:
        return (False, u'This rubric definition is not valid.')

    # No duplicate criteria names
    duplicates = _duplicates([criterion['name'] for criterion in rubric_dict['criteria']])
    if len(duplicates) > 0:
        msg = _(u"Criteria duplicate name(s): {duplicates}").format(
            duplicates=", ".join(duplicates)
        )
        return (False, msg)

    for criterion in rubric_dict['criteria']:
        # No duplicate option names within a criterion
        duplicates = _duplicates([option['name'] for option in criterion['options']])
        if len(duplicates) > 0:
            msg = _(u"Options in '{criterion}' have duplicate name(s): {duplicates}").format(
                criterion=criterion['name'], duplicates=", ".join(duplicates)
            )
            return (False, msg)

        # Some criteria may have no options, just written feedback.
        # In this case, written feedback must be required (not optional or disabled).
        if len(criterion['options']) == 0 and criterion.get('feedback', 'disabled') != 'required':
            msg = _(u'Criteria with no options must require written feedback.')
            return (False, msg)

        # Example-based assessments impose the additional restriction
        # that the point values for options must be unique within
        # a particular rubric criterion.
        if is_example_based:
            duplicates = _duplicates([option['points'] for option in criterion['options']])
            if len(duplicates) > 0:
                msg = _(u"Example-based assessments cannot have duplicate point values.")
                return (False, msg)

    # After a problem is released, authors are allowed to change text,
    # but nothing that would change the point value of a rubric.
    if is_released:

        # Number of criteria must be the same
        if len(rubric_dict['criteria']) != len(current_rubric['criteria']):
            return (False, _(u'The number of criteria cannot be changed after a problem is released.'))

        # Criteria names must be the same
        # We use criteria names as unique identifiers (unfortunately)
        # throughout the system.  Changing them mid-flight can cause
        # the grade page, for example, to raise 500 errors.
        # When we implement non-XML authoring, we might be able to fix this
        # the right way by assigning unique identifiers for criteria;
        # but for now, this is the safest way to avoid breaking problems
        # post-release.
        current_criterion_names = set(criterion.get('name') for criterion in current_rubric['criteria'])
        new_criterion_names = set(criterion.get('name') for criterion in rubric_dict['criteria'])
        if current_criterion_names != new_criterion_names:
            return (False, u'Criteria names cannot be changed after a problem is released')

        # Number of options for each criterion must be the same
        for new_criterion, old_criterion in _match_by_order(rubric_dict['criteria'], current_rubric['criteria']):
            if len(new_criterion['options']) != len(old_criterion['options']):
                return (False, _(u'The number of options cannot be changed after a problem is released.'))

            else:
                for new_option, old_option in _match_by_order(new_criterion['options'], old_criterion['options']):
                    if new_option['points'] != old_option['points']:
                        return (False, _(u'Point values cannot be changed after a problem is released.'))

    return (True, u'')


def validate_dates(start, end, date_ranges):
    """
    Check that start and due dates are valid.

    Args:
        start (str): ISO-formatted date string indicating when the problem opens.
        end (str): ISO-formatted date string indicating when the problem closes.
        date_ranges (list of tuples): List of (start, end) pair for each submission / assessment.

    Returns:
        tuple (is_valid, msg) where
            is_valid is a boolean indicating whether the assessment is semantically valid
            and msg describes any validation errors found.
    """
    try:
        resolve_dates(start, end, date_ranges)
    except (DateValidationError, InvalidDateFormat) as ex:
        return (False, unicode(ex))
    else:
        return (True, u'')


def validate_assessment_examples(rubric_dict, assessments):
    """
    Validate assessment training examples.

    Args:
        rubric_dict (dict): The serialized rubric model.
        assessments (list of dict): List of assessment dictionaries.

    Returns:
        tuple (is_valid, msg) where
            is_valid is a boolean indicating whether the assessment is semantically valid
            and msg describes any validation errors found.

    """
    for asmnt in assessments:
        if asmnt['name'] == 'student-training' or asmnt['name'] == 'example-based-assessment':

            examples = convert_training_examples_list_to_dict(asmnt['examples'])

            # Must have at least one training example
            if len(examples) == 0:
                return False, _(u"Student training and example-based assessments must have at least one training example")

            # Delegate to the student training API to validate the
            # examples against the rubric.
            errors = validate_training_examples(rubric_dict, examples)
            if errors:
                return False, "; ".join(errors)

    return True, u''


def validator(oa_block, strict_post_release=True):
    """
    Return a validator function configured for the XBlock.
    This will validate assessments, rubrics, and dates.

    Args:
        oa_block (OpenAssessmentBlock): The XBlock being updated.

    Kwargs:
        strict_post_release (bool): If true, restrict what authors can update once
            a problem has been released.

    Returns:
        callable, of a form that can be passed to `update_from_xml`.
    """

    def _inner(rubric_dict, assessments, submission_start=None, submission_due=None):

        is_released = strict_post_release and oa_block.is_released()

        # Assessments
        current_assessments = oa_block.rubric_assessments
        success, msg = validate_assessments(assessments, current_assessments, is_released)
        if not success:
            return (False, msg)

        # Rubric
        is_example_based = 'example-based-assessment' in [asmnt.get('name') for asmnt in assessments]
        current_rubric = {
            'prompt': oa_block.prompt,
            'criteria': oa_block.rubric_criteria
        }
        success, msg = validate_rubric(rubric_dict, current_rubric, is_released, is_example_based)
        if not success:
            return (False, msg)

        # Training examples
        success, msg = validate_assessment_examples(rubric_dict, assessments)
        if not success:
            return (False, msg)

        # Dates
        submission_dates = [(submission_start, submission_due)]
        assessment_dates = [(asmnt.get('start'), asmnt.get('due')) for asmnt in assessments]
        success, msg = validate_dates(oa_block.start, oa_block.due, submission_dates + assessment_dates)
        if not success:
            return (False, msg)

        # Success!
        return (True, u'')

    return _inner
