#pragma once

#include <string>

#include "core/ts.h"
#include "smt-switch/smt.h"

namespace pono {

/** Read predicates from a JSON file and construct SMT terms.
 *
 *  The JSON format is an array of predicate objects:
 *  [
 *    {
 *      "predicate_type": "eq",
 *      "operands": { "op1": "varname1", "op2": "varname2_or_constant" }
 *    },
 *    ...
 *  ]
 *
 *  Each operand is looked up in the transition system's named_terms().
 *  If not found, it is treated as an integer constant (e.g., "0").
 *
 *  @param json_path  path to the JSON predicate file
 *  @param ts         the transition system (provides variable name mappings)
 *  @param solver     the SMT solver (for constructing terms)
 *  @return a vector of predicate terms
 */
smt::TermVec read_predicates(const std::string & json_path,
                             const TransitionSystem & ts,
                             const smt::SmtSolver & solver);

}  // namespace pono