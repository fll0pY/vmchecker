#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Updates marks for modified results"""

from __future__ import with_statement

import os
import time


from . import paths
from . import repo_walker
from . import submissions
from . import penalty
from . import vmlogging
from .paths import VmcheckerPaths
from .config import CourseConfig
from .coursedb import opening_course_db
from .courselist import CourseList

_logger = vmlogging.create_module_logger('update_db')



def compute_late_penalty(assignment, account, vmcfg):
    """Returns the late submission penalty for this submission

    Computes the time penalty for the account, obtains the other
    penalties and bonuses from the grade_filename file
    and computes the final grade.

    """

    # The weights and limit are specific for each assignment
    # because you can have different weights and limit per
    # assignment
    weights = [float(x) for x in vmcfg.assignments().get(assignment, 'PenaltyWeights').split()]
    limit = int(vmcfg.assignments().get(assignment, 'PenaltyLimit'))

    sss = submissions.Submissions(VmcheckerPaths(vmcfg.root_path()))
    upload_time = sss.get_upload_time_struct(assignment, account)

    deadline = time.strptime(vmcfg.assignments().get(assignment, 'Deadline'),
                             penalty.DATE_FORMAT)
    holidays = int(vmcfg.get('vmchecker', 'Holidays'))

    if holidays != 0:
        holiday_start  = vmcfg.get('vmchecker', 'HolidayStart') .split(' , ')
        holiday_finish = vmcfg.get('vmchecker', 'HolidayFinish').split(' , ')
        penalty_value = penalty.compute_penalty(upload_time, deadline, 1,
                            weights, limit, holiday_start, holiday_finish)[0]
    else:
        penalty_value = penalty.compute_penalty(upload_time, deadline, 1,
                            weights, limit)[0]
    return (-penalty_value)


def compute_TA_penalty(grade_filename):
    """Compute the penalty assigned by the teaching assistant

    The grade_filename file can have any structure.

    The only rule is the following: any line that starts with a number
    with '-' or '+' is taken into account when computing the grade.

    An example for the file:
        +0.1 very good comments
        -0.2 possible leak of memory on line 234
        +0.1 treats exceptions
        -0.2 use of magic numbers

    """

    if not os.path.exists(grade_filename):
        return 0

    acc = 0
    with open(grade_filename) as handler:
        for line in handler.readlines():
            words = line.split()
            if len(words) == 0:
                continue
            fst_word = words[0].strip()
            try:
                # The first line may be of the form: '-1.0: my comment'.
                # This make the first word be '-1.0:' and that does not
                # parse as a float.
                acc += float(fst_word.split(':')[0])
            except ValueError:
                pass

    return acc


def compute_grade(assignment, user, grade_filename, vmcfg):
    """Returns the grade value after applying penalties and bonuses."""

    #if the file only contains one word(evaluation status) there's noting to compute
    with open(grade_filename) as f:
        lines = f.readlines()
        if len(lines) == 1 and len(lines[0].split()) == 1:
            # only one word in the file!
            return lines[0].split()[0]

    # Some courses don't grade on a 10 scale, so read the total number
    # of points for this assignment
    grade = float(vmcfg.assignments().get(assignment, 'TotalPoints'))
    grade += compute_TA_penalty(grade_filename)
    grade += compute_late_penalty(assignment, user, vmcfg)

    #at this point, grade is <= 0 if the homework didn't compile
    if grade <= 0:
        grade = 0

    return grade



def db_save_grade(assignment, account, submission_root,
                  vmcfg, course_db, ignore_timestamp = False):
    """Updates grade for the account's submission of assignment.

    Reads the grade's value only if the file containing the
    value was modified since the last update of the DB for this
    submission.

    """
    grade_filename = paths.submission_results_grade(submission_root)
    assignment_id = course_db.get_assignment_id(assignment)
    if assignment_id is None:
        assignment_id = course_db.add_assignment(assignment)

    user_id = None
    team_id = None
    isTeamAccount = False
    # First check if this is a team's mutual account
    vmpaths = paths.VmcheckerPaths(vmcfg.root_path())
    sss = submissions.Submissions(vmpaths)
    submitting_user = sss.get_submitting_user(assignment, account)
    if submitting_user is not None:
        # If there is a separate submitting user, then this is a team account
        isTeamAccount = True
        team_id = course_db.get_team_id(account)
        if team_id is None:
            team_id = course_db.add_team(account, True)
        submitting_user_id = course_db.get_user_id(submitting_user)
        if submitting_user_id is None:
            submitting_user_id = course_db.add_user(submitting_user)
        course_db.add_team_member(submitting_user_id, team_id)
        course_db.activate_team_for_assignment(team_id, assignment_id)

    if team_id is None:
        user_id = course_db.get_user_id(account)
        if user_id is None:
            user_id = course_db.add_user(account)

    db_mtime = course_db.get_grade_mtime(assignment_id, user_id = user_id, team_id = team_id)


    if os.path.exists(grade_filename):
        # we have the evaluation results for this homework
        mtime = os.path.getmtime(grade_filename)
        grade = compute_grade(assignment, account, grade_filename, vmcfg)
    elif os.path.exists(submission_root):
        # we don't have evaluation results, but the homework exists.
        # it must be in the tester's queue waiting to be evaluated.
        ignore_timestamp = True
        grade = submissions.STATUS_QUEUED
        mtime = 0
    else:
        # not evaluated and not even submitted. The student did not
        # send anything for this homework, so we don't fill this entry
        # in the grade table.
        return

    # only update grades for newer submissions than those already checked
    # or when forced to do so
    if db_mtime != mtime or ignore_timestamp:
        _logger.debug('Updating %s, %s (%s)', assignment, account, grade_filename)
        if not isTeamAccount:
            course_db.save_user_grade(assignment_id, user_id, grade, mtime)
        else:
            course_db.save_team_grade(assignment_id, team_id, grade, mtime)
        _logger.info('Updated %s, %s (%s) -- grade=%s', assignment, account, grade_filename, str(grade))
    else:
        _logger.info('SKIP (no tstamp change) %s, %s (%s)', assignment, account, grade_filename)





def update_grades(course_id, account = None, assignment = None,
        ignore_timestamp = False, simulate = False):
    """Update grades based on the given parameters.

        @account and @assignment can be used to narrow the search:

          * account==None, assignment==None -- compute all grades
          * account==None, assignment!=None -- all submissions for the assignment
          * account!=None, assignment==None -- all submissions from the account
          * account!=None, assignment!=None -- the account's last submission for the assignment
    """

    vmcfg   = CourseConfig(CourseList().course_config(course_id))
    vmpaths = paths.VmcheckerPaths(vmcfg.root_path())
    walker  = repo_walker.RepoWalker(vmcfg, simulate)
    db_file = vmpaths.db_file()

    with opening_course_db(db_file, isolation_level="EXCLUSIVE") as course_db:
        walker.walk(account, assignment, func=db_save_grade,
                    args=(vmcfg, course_db, ignore_timestamp))
