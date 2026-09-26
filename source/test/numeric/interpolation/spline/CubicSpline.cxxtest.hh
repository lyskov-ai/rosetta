// -*- mode:c++;tab-width:2;indent-tabs-mode:t;show-trailing-whitespace:t;rm-trailing-spaces:t -*-
// vi: set ts=2 noet:
//
// (c) Copyright Rosetta Commons Member Institutions.
// (c) This file is part of the Rosetta software suite and is made available under license.
// (c) The Rosetta software is developed by the contributing members of the Rosetta Commons.
// (c) For more information, see http://www.rosettacommons.org. Questions about this can be
// (c) addressed to University of Washington CoMotion, email: license@uw.edu.

/// @file   numeric/interpolation/spline
/// @brief  test suite for numeric::interpolation::spline::CubicSpline
/// @author Steven Combs (steven.combs@vanderbilt.edu)
/// This tests the functions that are in the cubic spline class.


// Test headers
#include <cxxtest/TestSuite.h>

// Unit headers
#include <numeric/interpolation/spline/CubicSpline.hh>
#include <numeric/interpolation/spline/CubicSpline.fwd.hh>


// --------------- Test Class --------------- //


class CubicSpline_tests : public CxxTest::TestSuite {


public:
	//shared data


	// --------------- Fixtures --------------- //

	// Define a test fixture (some initial state that several tests share)
	// In CxxTest, setUp()/tearDown() are executed around each test case. If you need a fixture on the test
	// suite level, i.e. something that gets constructed once before all the tests in the test suite are run,
	// suites have to be dynamically created. See CxxTest sample directory for example.

	// Shared initialization goes here.
	void setUp()
	{

	} //Match contents of Histogram_sample.hist


	// Shared finalization goes here.
	void tearDown() {

	}

	void test_cubic_spline_data_access(){


		const numeric::Real values[] =
		{
				26, 3, 1, 2, 1, 3, 6, 3, 8,
				2, 7, 8, 3, 4, 2, 1, 2, 5,
				30, 0, 2, 4, 6, 3, 4, 3, 3,
				4, 11, 5, 8, 5, 2, 0, 2, 2
		};
		const numeric::MathVector<numeric::Real> input_values(36, values);

		numeric::interpolation::spline::CubicSpline naturalspline;


		naturalspline.train(numeric::interpolation::spline::e_Natural, -180, 10, input_values, std::pair<numeric::Real, numeric::Real>(10,10));


		TS_ASSERT_DELTA(26.8509, naturalspline.F(-180.30) , .001);
		TS_ASSERT_DELTA(-2.83624,  naturalspline.dF(-180.30), .001 );

		TS_ASSERT_DELTA(1.15858,  naturalspline.F(180.30), .001 );
		TS_ASSERT_DELTA(-0.0816911,  naturalspline.dF(180.30), .001 );


		TS_ASSERT_DELTA(25.1493,  naturalspline.F(-179.70), .001 );
		TS_ASSERT_DELTA(-2.83479,  naturalspline.dF(-179.70), .001 );

		TS_ASSERT_DELTA(1.2076,  naturalspline.F(179.70), .001 );
		TS_ASSERT_DELTA(-0.0816911,  naturalspline.dF(179.70), .001 );


		TS_ASSERT_EQUALS(36,  naturalspline.get_dsecox().size() );
		TS_ASSERT_EQUALS(-180,  naturalspline.get_start() );
		TS_ASSERT_EQUALS(10, naturalspline.get_delta());
		TS_ASSERT_EQUALS(36, naturalspline.get_values().size());


	}

	// train() reuses the inverse it computed for the previous grid. Each spline below is
	// trained on a grid that differs from the one before it in one respect -- border, spacing
	// or size -- so a cache that ignored that respect would hand it the wrong inverse.
	void test_train_on_a_new_grid_does_not_reuse_the_previous_inverse(){
		using namespace numeric::interpolation::spline;

		const numeric::Real values[] =
		{
				26, 3, 1, 2, 1, 3, 6, 3, 8,
				2, 7, 8, 3, 4, 2, 1, 2, 5,
				30, 0, 2, 4, 6, 3, 4, 3, 3,
				4, 11, 5, 8, 5, 2, 0, 2, 2
		};
		const numeric::MathVector<numeric::Real> input_values(36, values);
		const numeric::MathVector<numeric::Real> first_half(18, values);
		const std::pair<numeric::Real, numeric::Real> firstbe(10, 10);

		CubicSpline natural_first, periodic, natural, coarser, shorter;
		natural_first.train(e_Natural, -180, 10, input_values, firstbe);
		periodic.train(e_Periodic, -180, 10, input_values, firstbe);
		natural.train(e_Natural, -180, 10, input_values, firstbe);
		coarser.train(e_Natural, -180, 20, input_values, firstbe);
		shorter.train(e_Natural, -180, 20, first_half, firstbe);

		// Natural to periodic: the first and last rows of the periodic system wrap around, which
		// the natural system's do not.
		const numeric::MathVector<numeric::Real> & s = periodic.get_dsecox();
		TS_ASSERT_DELTA((values[1] - 2 * values[0] + values[35]) / 10,
			10.0 / 6 * s(35) + 2 * 10.0 / 3 * s(0) + 10.0 / 6 * s(1), 1e-9);
		TS_ASSERT_DELTA((values[0] - 2 * values[35] + values[34]) / 10,
			10.0 / 6 * s(34) + 2 * 10.0 / 3 * s(35) + 10.0 / 6 * s(0), 1e-9);

		// Periodic to natural: the same values test_cubic_spline_data_access expects.
		TS_ASSERT_DELTA(26.8509, natural.F(-180.30), .001);
		TS_ASSERT_DELTA(-2.83624, natural.dF(-180.30), .001);
		TS_ASSERT_DELTA(1.15858, natural.F(180.30), .001);
		TS_ASSERT_DELTA(-0.0816911, natural.dF(180.30), .001);

		// Doubling the spacing: second derivatives scale as 1 / DELTA^2, so they quarter.
		for ( numeric::Size i = 0; i < 36; ++i ) {
			TS_ASSERT_DELTA(natural.get_dsecox()(i) / 4, coarser.get_dsecox()(i), 1e-12);
		}

		// Halving the size: a 36-point inverse would give 36 second derivatives.
		TS_ASSERT_EQUALS(18, shorter.get_dsecox().size());
	}


};
