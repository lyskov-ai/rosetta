// -*- mode:c++;tab-width:2;indent-tabs-mode:t;show-trailing-whitespace:t;rm-trailing-spaces:t -*-
// vi: set ts=2 noet:
//
// (c) Copyright Rosetta Commons Member Institutions.
// (c) This file is part of the Rosetta software suite and is made available under license.
// (c) The Rosetta software is developed by the contributing members of the Rosetta Commons.
// (c) For more information, see http://www.rosettacommons.org. Questions about this can be
// (c) addressed to University of Washington CoMotion, email: license@uw.edu.

/// @file   test/core/chemical/mainchain_potential/MainchainScoreTableTests.cxxtest.hh
/// @brief  Unit tests for reading a mainchain potential map into a MainchainScoreTable.

// Test headers
#include <cxxtest/TestSuite.h>
#include <test/core/init_util.hh>

// Unit headers
#include <core/chemical/mainchain_potential/MainchainScoreTable.hh>

// Utility headers
#include <utility/excn/Exceptions.hh>

// C++ headers
#include <cmath>
#include <sstream>
#include <string>

namespace {

/// @brief A two-torsion map for ALA with an equal probability at every point of its grid.
std::string
flat_ala_map( core::Size const phi_points, core::Size const psi_points ) {
	std::ostringstream map;
	map << "@N_MAINCHAIN_TORSIONS 2\n";
	map << "@DIMENSIONS " << phi_points << " " << psi_points << "\n";
	core::Real const prob( 1.0 / static_cast< core::Real >( phi_points * psi_points ) );
	for ( core::Size i( 0 ); i < phi_points; ++i ) {
		for ( core::Size j( 0 ); j < psi_points; ++j ) {
			map << "ALA " << i * 360.0 / phi_points << " " << j * 360.0 / psi_points << " " << prob << " " << -std::log( prob ) << "\n";
		}
	}
	return map.str();
}

/// @brief Parse a map for ALA, and return the message of the exception it raised, or "" if it raised none.
std::string
parse_error( std::string const & map ) {
	core::chemical::mainchain_potential::MainchainScoreTable table;
	try {
		table.parse_rama_map_file_shapovalov( "test.rama", map, "ALA", true );
	} catch ( utility::excn::Exception const & e ) {
		return e.msg();
	}
	return "";
}

}

class MainchainScoreTableTests : public CxxTest::TestSuite {

public:

	void setUp() {
		core_init();
	}

	void test_reads_a_36_by_36_map() {
		TS_ASSERT_EQUALS( parse_error( flat_ala_map( 36, 36 ) ), "" );
	}

	void test_accepts_2_and_360_points_per_torsion() {
		TS_ASSERT_EQUALS( parse_error( flat_ala_map( 2, 360 ) ), "" );
	}

	void test_rejects_1_point_for_a_torsion() {
		TS_ASSERT( parse_error( flat_ala_map( 1, 36 ) ).find( "file's \"@DIMENSIONS\" line sets the number of grid points for mainchain torsion 1 to 1, outside the allowed range of 2 to 360." ) != std::string::npos );
	}

	void test_rejects_361_points_for_a_torsion() {
		TS_ASSERT( parse_error( flat_ala_map( 36, 361 ) ).find( "file's \"@DIMENSIONS\" line sets the number of grid points for mainchain torsion 2 to 361, outside the allowed range of 2 to 360." ) != std::string::npos );
	}

	/// @details A negative count reads as a huge unsigned one.
	void test_rejects_a_negative_number_of_points() {
		TS_ASSERT( parse_error( "@N_MAINCHAIN_TORSIONS 2\n@DIMENSIONS 36 -1\n" ).find( "grid points for mainchain torsion 2 to " + std::to_string( core::Size( -1 ) ) + ", outside the allowed range" ) != std::string::npos );
	}

	/// @details 256^4 points is 2^32, which wraps a 32-bit size_t to zero.
	void test_rejects_a_grid_of_256_to_the_4_points() {
		TS_ASSERT( parse_error( "@N_MAINCHAIN_TORSIONS 4\n@DIMENSIONS 256 256 256 256\n" ).find( "file's \"@DIMENSIONS\" line describes is too large.  A spline stores 2^N values for each point of an N-dimensional grid, and may store at most 33554432." ) != std::string::npos );
	}

	/// @details 256 x 128 x 129 points, times 2^3, is 33816576 values.
	void test_rejects_a_spline_of_just_over_2_to_the_25_values() {
		TS_ASSERT( parse_error( "@N_MAINCHAIN_TORSIONS 3\n@DIMENSIONS 256 128 129\n" ).find( "line describes is too large." ) != std::string::npos );
	}

	/// @details 256 x 128 x 128 points, times 2^3, is 2^25 values.  Fitting a spline that large
	/// would be slow, so the map's "@OFFSETS" line has one entry too many, which stops the parse
	/// after the "@DIMENSIONS" line is accepted and before any spline is fitted.
	void test_accepts_a_spline_of_2_to_the_25_values() {
		TS_ASSERT( parse_error( "@N_MAINCHAIN_TORSIONS 3\n@DIMENSIONS 256 128 128\n@OFFSETS 0 0 0 0\n" ).find( "Too many dimensions were specified in an \"@OFFSETS\" line." ) != std::string::npos );
	}

};
